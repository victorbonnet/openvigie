import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openvigie.adapters.base import HostContext, ProcessInfo
from openvigie.adapters.hermes import HermesAdapter
from openvigie.adapters.hermes.adapter import is_hermes_process
from openvigie.adapters.registry import discover
from openvigie.adapters.testing import AdapterContract, fake_context, fake_process
from openvigie.model import HealthState, SandboxState

FIXTURE = Path(__file__).parent / "fixtures" / "0.21.7" / "home" / ".hermes"
NEWEST_MESSAGE = datetime(2026, 10, 14, 11, 58, 10, tzinfo=UTC)


def make_home(tmp_path: Path, name: str = "home") -> Path:
    home = tmp_path / name
    shutil.copytree(FIXTURE, home / ".hermes")
    return home


def gateway(home: Path, pid: int = 1302) -> ProcessInfo:
    python = f"{home}/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3"
    return fake_process(pid, "hermes", (python, "-I", "-c", "import os", "gateway", "run"),
                        exe=python)  # fmt: skip


def helper(home: Path, pid: int = 1310) -> ProcessInfo:
    python = f"{home}/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3"
    script = f"{home}/.hermes/hermes-agent/tools/mcp_death_supervisor.py"
    return fake_process(pid, "python3", (python, script, "--parent-pgid", "1302"), exe=python)


class TestHermesContract(AdapterContract):
    adapter_cls = HermesAdapter

    def scenarios(self, tmp_path: Path) -> dict[str, HostContext]:
        home = make_home(tmp_path)
        return {
            "installed": fake_context(homes=[home]),
            "running": fake_context(homes=[home], processes=[gateway(home), helper(home)]),
        }


def test_detects_running_gateway(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    ctx = fake_context(
        homes=[home],
        processes=[gateway(home), helper(home), fake_process(9, "bash")],
        sandboxed={1302: (SandboxState.NO, None)},
    )
    adapter = HermesAdapter()
    (agent,) = adapter.detect(ctx)
    assert agent.name == "Hermes"
    assert agent.version == "0.21.7"
    assert agent.config_root == "~/.hermes"
    assert agent.process_running is True
    assert agent.sandboxed is SandboxState.NO
    assert agent.last_activity_at is not None
    assert agent.last_activity_at.replace(microsecond=0) == NEWEST_MESSAGE
    assert [p.pid for p in adapter.processes(agent.agent_id)] == [1302, 1310]
    assert adapter.health().state is HealthState.OK


def test_stopped_gateway(tmp_path: Path) -> None:
    (agent,) = HermesAdapter().detect(fake_context(homes=[make_home(tmp_path)]))
    assert agent.process_running is False
    assert agent.version == "0.21.7"


def test_not_installed(tmp_path: Path) -> None:
    (tmp_path / "home" / ".hermes").mkdir(parents=True)
    assert HermesAdapter().detect(fake_context(homes=[tmp_path / "home"])) == []


def test_malformed_config_degrades_but_still_detects(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / ".hermes" / "config.yaml").write_text("model: [unclosed\n")
    adapter = HermesAdapter()
    (agent,) = adapter.detect(fake_context(homes=[home]))
    assert agent.version == "0.21.7"
    health = adapter.health()
    assert health.state is HealthState.DEGRADED
    assert health.reason == "config.yaml is not valid YAML"


def test_unreadable_state_db_degrades(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / ".hermes" / "state.db").write_bytes(b"not a database" * 100)
    adapter = HermesAdapter()
    (agent,) = adapter.detect(fake_context(homes=[home]))
    assert agent.last_activity_at is None
    assert adapter.health().state is HealthState.DEGRADED


def test_empty_message_store_has_no_activity(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    with sqlite3.connect(home / ".hermes" / "state.db") as conn:
        conn.execute("delete from messages")
    (agent,) = HermesAdapter().detect(fake_context(homes=[home]))
    assert agent.last_activity_at is None


def test_hermes_home_override(tmp_path: Path) -> None:
    custom = tmp_path / "hermes-home"
    shutil.copytree(FIXTURE, custom)
    ctx = fake_context(homes=[tmp_path / "nohome"], env={"HERMES_HOME": str(custom)})
    (agent,) = HermesAdapter().detect(ctx)
    assert agent.config_root == str(custom)


def test_credentials_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_home(tmp_path) / ".hermes"
    (root / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n")
    (root / "auth.json").write_text('{"token": "x"}')
    (root / "auth").mkdir()
    (root / "auth" / "profile.json").write_text("{}")
    opened: list[str] = []
    real_open = os.open

    def spy(path: object, *args: object, **kwargs: object) -> int:
        opened.append(str(path))
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", spy)
    HermesAdapter().detect(fake_context(homes=[root.parent]))
    assert opened
    assert not any(p.endswith((".env", "auth.json")) or "/auth/" in p for p in opened)


def test_process_matching(tmp_path: Path) -> None:
    roots = [tmp_path / "home" / ".hermes"]
    pip_install = fake_process(5, "hermes", ("/usr/bin/python3", "/usr/local/bin/hermes"),
                               exe="/usr/bin/python3.12")  # fmt: skip
    js_engine = fake_process(6, "hermes", ("hermes", "app.js"), exe="/usr/local/bin/hermes")
    assert is_hermes_process(gateway(tmp_path / "home"), roots)
    assert is_hermes_process(helper(tmp_path / "home"), roots)
    assert is_hermes_process(pip_install, roots)
    assert not is_hermes_process(js_engine, roots)  # Meta's Hermes JS engine shares the name
    assert not is_hermes_process(fake_process(7, "python3", ("python3", "app.py")), roots)


def test_two_homes_get_their_own_processes(tmp_path: Path) -> None:
    alice, bob = make_home(tmp_path, "alice"), make_home(tmp_path, "bob")
    ctx = fake_context(homes=[alice, bob], processes=[gateway(alice, 100), gateway(bob, 200)])
    adapter = HermesAdapter()
    agents = adapter.detect(ctx)
    pids = {adapter.roots[a.agent_id]: [p.pid for p in adapter.processes(a.agent_id)]
            for a in agents}  # fmt: skip
    assert pids == {alice / ".hermes": [100], bob / ".hermes": [200]}


def test_registered() -> None:
    assert "hermes" in [a.agent_type for a in discover(quiet=True).adapters]
