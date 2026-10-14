import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

from openvigie.adapters.caps import pinned
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.testing import fake_context, fake_process
from openvigie.model import ApprovalLevel, Capabilities, FsAccess, NetAccess, to_dict

FIXTURE = Path(__file__).parent / "fixtures" / "capabilities" / "home"
FAKE_VALUES = ("ghp_FAKE", "FAKEKEY123", "Bearer FAKE", "someone@example.com", "deadbeef")


def home_copy(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    shutil.copytree(FIXTURE, home)
    state = home / ".claude.json"
    state.write_text(state.read_text().replace("{HOME}", str(home)))
    return home


def capabilities(home: Path, *procs: object) -> Capabilities:
    ctx = fake_context(homes=[home], processes=procs)  # type: ignore[arg-type]
    adapter = ClaudeCodeAdapter()
    (agent,) = adapter.detect(ctx)
    caps = adapter.capabilities(ctx, agent)
    assert caps is not None
    return caps


def test_fixture(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    caps = capabilities(home_copy(tmp_path))
    # settings.local.json wins for the mode, deny lists are combined
    assert caps.approvals is ApprovalLevel.NEVER_ASK and caps.approval_mode == "bypassPermissions"
    assert caps.tools_denied == ("Read(./.env)", "WebFetch", "Bash(rm -rf:*)")
    assert caps.shell is True  # Bash is only partly denied
    assert caps.filesystem is FsAccess.FULL and caps.network is NetAccess.FULL  # no sandbox
    assert caps.writable_roots == ("~/shared",)
    servers = {s.name: s for s in caps.mcp_servers}
    assert set(servers) == {"github", "db", "docs"}
    assert servers["github"].command == "npx" and servers["github"].pinned is False
    assert servers["github"].secret_names == ("GITHUB_PERSONAL_ACCESS_TOKEN",)
    assert servers["db"].command == "uvx" and servers["db"].pinned is True
    assert servers["docs"].transport == "http" and servers["docs"].url_host == "mcp.example.com"
    assert caps.skills == ("pdf",)
    assert [(h.event, h.command) for h in caps.hooks] == [("PreToolUse", "audit-bash")]
    assert {s.name for s in caps.secrets} == {"GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN",
                                              "Authorization"}  # fmt: skip
    assert "~/.claude/settings.json" in caps.sources and "~/src/app/.mcp.json" in caps.sources
    wire = json.dumps(to_dict(caps)) + caplog.text
    for fake in FAKE_VALUES:
        assert fake not in wire


def test_bypass_flag_on_the_command_line_wins(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"permissions": {"defaultMode": "default"}}')
    proc = fake_process(7, "claude", cmdline=("claude", "--dangerously-skip-permissions"))
    assert capabilities(home, proc).approvals is ApprovalLevel.NEVER_ASK
    assert capabilities(home).approvals is ApprovalLevel.ASK


def test_sandbox_and_missing_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    settings = {"sandbox": {"enabled": True, "network": {"allowedDomains": ["github.com"]}},
                "permissions": {"deny": ["Bash"]}}  # fmt: skip
    (home / ".claude" / "settings.json").write_text(json.dumps(settings))
    caps = capabilities(home)
    assert caps.filesystem is FsAccess.WORKSPACE and caps.network is NetAccess.ALLOWLIST
    assert caps.network_allowlist == ("github.com",) and caps.shell is False
    assert caps.mcp_servers == () and caps.skills == ()


def test_broken_and_symlinked_files_are_skipped(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{not json")
    secret = tmp_path / "elsewhere.json"
    secret.write_text('{"mcpServers": {"x": {"command": "npx"}}}')
    (home / ".claude.json").symlink_to(secret)
    caps = capabilities(home)
    assert caps.mcp_servers == () and caps.sources == ()


@pytest.mark.parametrize(
    ("command", "args", "expected"),
    [
        ("npx", ["-y", "@scope/pkg@1.2.3"], True),
        ("npx", ["-y", "@scope/pkg"], False),
        ("npx", ["pkg@latest"], False),
        ("uvx", ["tool==0.3"], True),
        ("pnpm", ["dlx", "pkg@2"], True),
        ("node", ["server.js"], None),
        ("npx", [], False),
    ],
)
def test_pinned(command: str, args: list[str], expected: bool | None) -> None:
    assert pinned(command, args) is expected


def test_unchanged_files_are_not_parsed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openvigie.adapters.claude_code import capabilities as caps_module

    home = home_copy(tmp_path)
    ctx = fake_context(homes=[home])
    adapter = ClaudeCodeAdapter()
    (agent,) = adapter.detect(ctx)
    calls: list[Path] = []
    real_load = caps_module._load

    def counting_load(path: Path, *args: Any) -> dict[str, Any]:
        calls.append(path)
        return real_load(path, *args)

    monkeypatch.setattr(caps_module, "_load", counting_load)
    first = adapter.capabilities(ctx, agent)
    reads = len(calls)
    assert adapter.capabilities(ctx, agent) == first and len(calls) == reads
    settings = home / ".claude" / "settings.json"
    settings.write_text(settings.read_text().replace("acceptEdits", "default") + " ")
    adapter.capabilities(ctx, agent)
    assert len(calls) == 2 * reads
