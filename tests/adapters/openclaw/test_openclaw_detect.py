import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openvigie.adapters.base import HostContext, ProcessInfo
from openvigie.adapters.openclaw import OpenClawAdapter
from openvigie.adapters.openclaw.adapter import is_openclaw_process
from openvigie.adapters.registry import discover
from openvigie.adapters.testing import AdapterContract, fake_context, fake_process
from openvigie.model import AgentInstance, HealthState, SandboxState

FIXTURE = Path(__file__).parent / "fixtures" / "2026.9.4" / "home" / ".openclaw"
OLDER = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
NEWER = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
GATEWAY_ARGS = ("/usr/bin/node", "{script}", "gateway", "--port", "18789")


def make_home(tmp_path: Path, name: str = "home") -> Path:
    home = tmp_path / name
    shutil.copytree(FIXTURE, home / ".openclaw")
    for path in (home / ".openclaw" / "agents").rglob("*"):
        when = NEWER if path.name.endswith(".trajectory.jsonl") else OLDER
        os.utime(path, (when.timestamp(), when.timestamp()))
    return home


def gateway(script: str, pid: int = 4242) -> ProcessInfo:
    args = [a.format(script=script) for a in GATEWAY_ARGS]
    return fake_process(pid, "MainThread", args, exe="/usr/bin/node")


def install_package(tmp_path: Path, version: str) -> str:
    package = tmp_path / "npm-global" / "lib" / "node_modules" / "openclaw"
    (package / "dist").mkdir(parents=True)
    (package / "dist" / "index.js").write_text("")
    (package / "package.json").write_text(json.dumps({"name": "openclaw", "version": version}))
    return str(package / "dist" / "index.js")


class TestOpenClawContract(AdapterContract):
    adapter_cls = OpenClawAdapter

    def scenarios(self, tmp_path: Path) -> dict[str, HostContext]:
        home = make_home(tmp_path)
        script = install_package(tmp_path, "2026.9.4")
        return {
            "installed": fake_context(homes=[home]),
            "running": fake_context(homes=[home], processes=[gateway(script)]),
        }


def by_name(agents: list[AgentInstance]) -> dict[str, AgentInstance]:
    return {a.name: a for a in agents}


def test_one_row_per_agent_of_a_running_gateway(tmp_path: Path) -> None:
    script = install_package(tmp_path, "2026.9.4")
    ctx = fake_context(
        homes=[make_home(tmp_path)],
        processes=[gateway(script)],
        sandboxed={4242: (SandboxState.NO, None)},
    )
    adapter = OpenClawAdapter()
    agents = by_name(adapter.detect(ctx))
    assert sorted(agents) == ["OpenClaw · lutra", "OpenClaw · main", "OpenClaw · vulpes"]
    assert len({a.agent_id for a in agents.values()}) == 3
    main = agents["OpenClaw · main"]
    assert main.config_root == "~/.openclaw/agents/main"
    assert main.version == "2026.9.4"
    assert main.process_running is True
    assert main.sandboxed is SandboxState.NO
    assert main.last_activity_at == NEWER
    assert agents["OpenClaw · vulpes"].last_activity_at == OLDER
    assert adapter.health().state is HealthState.OK


def test_installed_package_version_wins_over_config(tmp_path: Path) -> None:
    # the config's meta.lastTouchedVersion lags until a newer version rewrites the config
    script = install_package(tmp_path, "2026.9.5")
    ctx = fake_context(homes=[make_home(tmp_path)], processes=[gateway(script)])
    assert {a.version for a in OpenClawAdapter().detect(ctx)} == {"2026.9.5"}


def test_stopped_gateway(tmp_path: Path) -> None:
    agents = OpenClawAdapter().detect(fake_context(homes=[make_home(tmp_path)]))
    assert len(agents) == 3
    assert all(a.process_running is False for a in agents)
    assert {a.version for a in agents} == {"2026.9.4"}


def test_not_installed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".openclaw" / "unrelated").mkdir(parents=True)
    assert OpenClawAdapter().detect(fake_context(homes=[home])) == []


def test_state_dir_override(tmp_path: Path) -> None:
    custom = tmp_path / "state"
    shutil.copytree(FIXTURE, custom)
    ctx = fake_context(homes=[tmp_path / "nohome"], env={"OPENCLAW_STATE_DIR": str(custom)})
    agents = OpenClawAdapter().detect(ctx)
    assert {a.config_root for a in agents} == {
        f"{custom}/agents/{n}" for n in ("main", "lutra", "vulpes")
    }


def test_json5_config_degrades_but_still_detects(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / ".openclaw" / "openclaw.json").write_text(
        "{\n  // edited by hand\n  gateway: { port: 18789, },\n"
        '  meta: { lastTouchedVersion: "2026.9.4" },\n}\n'
    )
    adapter = OpenClawAdapter()
    agents = adapter.detect(fake_context(homes=[home]))
    assert len(agents) == 3
    assert {a.version for a in agents} == {None}
    health = adapter.health()
    assert health.state is HealthState.DEGRADED
    assert health.reason == "openclaw.json is not strict JSON (JSON5?): version unknown"


def test_session_database_counts_as_activity(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    db = home / ".openclaw" / "agents" / "lutra" / "agent" / "openclaw-agent.sqlite"
    os.utime(db, (NEWER.timestamp(), NEWER.timestamp()))
    agents = by_name(OpenClawAdapter().detect(fake_context(homes=[home])))
    assert agents["OpenClaw · lutra"].last_activity_at == NEWER


def test_agent_ids_from_config_and_folders(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    shutil.rmtree(home / ".openclaw" / "agents" / "lutra")
    (home / ".openclaw" / "agents" / ".hidden").mkdir()
    agents = by_name(OpenClawAdapter().detect(fake_context(homes=[home])))
    assert sorted(agents) == ["OpenClaw · lutra", "OpenClaw · main", "OpenClaw · vulpes"]
    assert agents["OpenClaw · lutra"].last_activity_at is None


def test_credentials_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = make_home(tmp_path)
    root = home / ".openclaw"
    secrets = [
        root / ".env",
        root / "credentials" / "telegram.json",
        root / "identity" / "device.json",
        root / "agents" / "main" / "agent" / "auth-profiles.json",
        root / "agents" / "main" / "agent" / "auth-state.json",
    ]
    for path in secrets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"token": "x"}')
    script = install_package(tmp_path, "2026.9.4")
    opened: list[str] = []
    real_open = os.open

    def spy(path: object, *args: object, **kwargs: object) -> int:
        opened.append(str(path))
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", spy)
    OpenClawAdapter().detect(fake_context(homes=[home], processes=[gateway(script)]))
    assert opened
    assert all(p.endswith(("openclaw.json", "package.json")) for p in opened), opened


def test_gateway_processes_not_offered_for_stop(tmp_path: Path) -> None:
    script = install_package(tmp_path, "2026.9.4")
    adapter = OpenClawAdapter()
    agents = adapter.detect(fake_context(homes=[make_home(tmp_path)], processes=[gateway(script)]))
    assert all(adapter.processes(a.agent_id) == () for a in agents)


@pytest.mark.parametrize(
    ("name", "args", "exe", "expected"),
    [
        (
            "MainThread",
            (
                "/usr/bin/node",
                "/home/u/.npm-global/lib/node_modules/openclaw/dist/index.js",
                "gateway",
            ),
            "/usr/bin/node",
            True,
        ),
        (
            "node",
            ("node", "/opt/homebrew/lib/node_modules/openclaw/dist/index.js"),
            "/opt/homebrew/bin/node",
            True,
        ),
        ("openclaw", ("openclaw", "tui"), "/home/u/.npm-global/bin/openclaw", True),
        ("clawdbot", ("clawdbot",), "/usr/local/bin/clawdbot", True),
        ("node", ("node", "/srv/app/dist/index.js"), "/usr/bin/node", False),
        ("MainThread", ("/usr/bin/python3", "server.py"), "/usr/bin/python3", False),
    ],
)
def test_process_signature(name: str, args: tuple[str, ...], exe: str, expected: bool) -> None:
    assert is_openclaw_process(fake_process(1, name, args, exe=exe)) is expected


def test_registered_as_builtin() -> None:
    assert "openclaw" in {a.agent_type for a in discover(quiet=True).adapters}
