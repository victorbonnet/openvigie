import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openvigie.adapters.base import HostContext
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.codex.adapter import is_codex_process
from openvigie.adapters.registry import discover
from openvigie.adapters.testing import AdapterContract, fake_context, fake_process
from openvigie.model import HealthState, SandboxState

FIXTURE = Path(__file__).parent / "fixtures" / "home" / ".codex"
OLDER = datetime(2026, 10, 13, 18, 31, tzinfo=UTC)
NEWER = datetime(2026, 10, 14, 9, 1, tzinfo=UTC)


def make_home(tmp_path: Path, name: str = "home") -> Path:
    home = tmp_path / name
    shutil.copytree(FIXTURE, home / ".codex")
    for path in (home / ".codex" / "sessions").rglob("*.jsonl"):
        when = NEWER if "2026-10-14" in path.name else OLDER
        os.utime(path, (when.timestamp(), when.timestamp()))
    return home


class TestCodexContract(AdapterContract):
    adapter_cls = CodexAdapter

    def scenarios(self, tmp_path: Path) -> dict[str, HostContext]:
        home = make_home(tmp_path)
        proc = fake_process(300, "codex", exe="/usr/local/bin/codex")
        return {
            "installed": fake_context(homes=[home]),
            "running": fake_context(homes=[home], processes=[proc]),
        }


def test_detects_running_codex_with_version_from_newest_rollout(tmp_path: Path) -> None:
    ctx = fake_context(
        homes=[make_home(tmp_path)],
        processes=[fake_process(300, "codex", exe="/usr/local/bin/codex")],
        sandboxed={300: (SandboxState.YES, "seatbelt")},
    )
    adapter = CodexAdapter()
    (agent,) = adapter.detect(ctx)
    assert agent.version == "0.46.0"
    assert agent.last_activity_at == NEWER
    assert agent.process_running is True
    assert agent.config_root == "~/.codex"
    assert (agent.sandboxed, agent.sandbox_kind) == (SandboxState.YES, "seatbelt")
    assert adapter.health().state is HealthState.OK


def test_idle_install(tmp_path: Path) -> None:
    (agent,) = CodexAdapter().detect(fake_context(homes=[make_home(tmp_path)]))
    assert agent.process_running is False


def test_codex_home_override(tmp_path: Path) -> None:
    custom = tmp_path / "codex-home"
    shutil.copytree(FIXTURE, custom)
    ctx = fake_context(homes=[tmp_path / "nohome"], env={"CODEX_HOME": str(custom)})
    (agent,) = CodexAdapter().detect(ctx)
    assert agent.config_root == str(custom)


def test_malformed_config_degrades_but_still_detects(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / ".codex" / "config.toml").write_text("model = = broken\n")
    adapter = CodexAdapter()
    (agent,) = adapter.detect(fake_context(homes=[home]))
    assert agent.version == "0.46.0"
    health = adapter.health()
    assert health.state is HealthState.DEGRADED
    assert health.reason == "config.toml is not valid TOML"


def test_auth_json_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = make_home(tmp_path)
    (home / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "x"}')
    opened: list[str] = []
    real_open = os.open

    def spy(path: object, *args: object, **kwargs: object) -> int:
        opened.append(str(path))
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", spy)
    CodexAdapter().detect(fake_context(homes=[home]))
    assert opened  # the spy saw the reads it should
    assert not any(p.endswith("auth.json") for p in opened)


def test_only_auth_json_still_counts_as_install(tmp_path: Path) -> None:
    root = tmp_path / "home" / ".codex"
    root.mkdir(parents=True)
    (root / "auth.json").write_text("{}")
    (agent,) = CodexAdapter().detect(fake_context(homes=[tmp_path / "home"]))
    assert agent.version is None and agent.last_activity_at is None


@pytest.mark.parametrize(
    ("name", "exe", "cmdline", "expected"),
    [
        ("codex", "/usr/local/bin/codex", ["codex"], True),
        ("codex-aarch64-a", "/x/codex-aarch64-apple-darwin", ["codex"], True),
        ("node", "/usr/bin/node", ["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"], True),  # noqa: E501
        ("node", "/usr/bin/node", ["node", "app.js"], False),
        ("codexctl", "/usr/bin/codexctl", ["codexctl"], False),
    ],
)  # fmt: skip
def test_process_matching(name: str, exe: str, cmdline: list[str], expected: bool) -> None:
    assert is_codex_process(fake_process(1, name, cmdline, exe=exe)) is expected


def test_registered_as_builtin() -> None:
    assert "codex" in [a.agent_type for a in discover(quiet=True).adapters]
