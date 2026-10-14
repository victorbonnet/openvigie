import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openvigie.adapters.base import HostContext
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.claude_code.adapter import is_claude_process
from openvigie.adapters.registry import discover
from openvigie.adapters.testing import AdapterContract, fake_context, fake_process
from openvigie.model import SandboxState

FIXTURE = Path(__file__).parent / "fixtures" / "home" / ".claude"
MTIME = datetime(2026, 10, 14, 11, 58, tzinfo=UTC)


def make_home(tmp_path: Path, name: str = "home") -> Path:
    home = tmp_path / name
    shutil.copytree(FIXTURE, home / ".claude")
    for path in (home / ".claude").rglob("*.jsonl"):
        os.utime(path, (MTIME.timestamp(), MTIME.timestamp()))
    return home


def running(pid: int = 97) -> object:
    return fake_process(pid, "claude", exe="/opt/claude-code/bin/claude")


class TestClaudeCodeContract(AdapterContract):
    adapter_cls = ClaudeCodeAdapter

    def scenarios(self, tmp_path: Path) -> dict[str, HostContext]:
        home = make_home(tmp_path)
        return {
            "installed": fake_context(homes=[home]),
            "running": fake_context(homes=[home], processes=[running()]),  # type: ignore[list-item]
            "two-homes": fake_context(homes=[home, make_home(tmp_path, "other")]),
        }


def test_detects_installed_and_running(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    ctx = fake_context(
        homes=[home],
        processes=[running(), fake_process(5, "bash")],  # type: ignore[list-item]
        sandboxed={97: (SandboxState.YES, "bwrap")},
    )
    (agent,) = ClaudeCodeAdapter().detect(ctx)
    assert agent.name == "Claude Code"
    assert agent.version == "2.1.299"
    assert agent.config_root == "~/.claude"
    assert agent.process_running is True
    assert agent.last_activity_at == MTIME
    assert (agent.sandboxed, agent.sandbox_kind) == (SandboxState.YES, "bwrap")


def test_installed_not_running(tmp_path: Path) -> None:
    (agent,) = ClaudeCodeAdapter().detect(fake_context(homes=[make_home(tmp_path)]))
    assert agent.process_running is False
    assert agent.sandboxed is SandboxState.UNKNOWN


def test_empty_claude_dir_is_not_an_install(tmp_path: Path) -> None:
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    assert ClaudeCodeAdapter().detect(fake_context(homes=[tmp_path / "home"])) == []


def test_config_dir_override(tmp_path: Path) -> None:
    custom = tmp_path / "custom-claude"
    shutil.copytree(FIXTURE, custom)
    ctx = fake_context(homes=[tmp_path / "nohome"], env={"CLAUDE_CONFIG_DIR": str(custom)})
    (agent,) = ClaudeCodeAdapter().detect(ctx)
    assert agent.config_root == str(custom)


def test_two_homes_give_two_stable_agents(tmp_path: Path) -> None:
    ctx = fake_context(homes=[make_home(tmp_path, "a"), make_home(tmp_path, "b")])
    agents = ClaudeCodeAdapter().detect(ctx)
    assert len(agents) == 2
    assert len({a.agent_id for a in agents}) == 2


def test_symlinked_root_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").symlink_to(FIXTURE)
    assert ClaudeCodeAdapter().detect(fake_context(homes=[home])) == []


def test_credentials_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = make_home(tmp_path)
    (home / ".claude" / ".credentials.json").write_text('{"token": "x"}')
    opened: list[str] = []
    real_open = os.open

    def spy(path: object, *args: object, **kwargs: object) -> int:
        opened.append(str(path))
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", spy)
    ClaudeCodeAdapter().detect(fake_context(homes=[home]))
    assert not any(p.endswith(".credentials.json") for p in opened)


@pytest.mark.parametrize(
    ("name", "exe", "cmdline", "expected"),
    [
        ("claude", "/opt/claude-code/bin/claude", ["claude"], True),
        ("node", "/usr/bin/node", ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"], True),  # noqa: E501
        ("node", "/usr/bin/node", ["node", "server.js"], False),
        ("claude-desktop", "/Applications/Claude.app/x", ["x"], False),
    ],
)  # fmt: skip
def test_process_matching(name: str, exe: str, cmdline: list[str], expected: bool) -> None:
    assert is_claude_process(fake_process(1, name, cmdline, exe=exe)) is expected


def test_registered_as_builtin() -> None:
    assert "claude-code" in [a.agent_type for a in discover(quiet=True).adapters]
