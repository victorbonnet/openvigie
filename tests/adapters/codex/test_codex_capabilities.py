import json
import shutil
from pathlib import Path

import pytest

from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.testing import fake_context, fake_process
from openvigie.model import ApprovalLevel, Capabilities, FsAccess, NetAccess, to_dict

FIXTURE = Path(__file__).parent / "fixtures" / "home" / ".codex"


def capabilities(tmp_path: Path, config: str | None = None, *procs: object) -> Capabilities:
    home = tmp_path / "home"
    shutil.copytree(FIXTURE, home / ".codex")
    if config is not None:
        (home / ".codex" / "config.toml").write_text(config)
    ctx = fake_context(homes=[home], processes=procs)  # type: ignore[arg-type]
    adapter = CodexAdapter()
    (agent,) = adapter.detect(ctx)
    caps = adapter.capabilities(ctx, agent)
    assert caps is not None
    return caps


def test_recorded_fixture_config(tmp_path: Path) -> None:
    caps = capabilities(tmp_path)
    assert caps.approvals is ApprovalLevel.AUTO_EDIT and caps.approval_mode == "on-request"
    assert caps.filesystem is FsAccess.WORKSPACE and caps.network is NetAccess.NONE
    (server,) = caps.mcp_servers
    assert (server.name, server.command, server.pinned) == ("docs", "npx", False)
    assert caps.sources == ("~/.codex/config.toml",) and caps.shell is True


def test_full_access_never_ask_and_secrets(tmp_path: Path) -> None:
    config = """
approval_policy = "on-request"
sandbox_mode = "workspace-write"
profile = "yolo"

[profiles.yolo]
approval_policy = "never"
sandbox_mode = "danger-full-access"

[profiles.yolo.mcp_servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github@0.6.2"]
env = { GITHUB_TOKEN = "ghp_FAKE2222222222222222222222222222222222" }

[profiles.yolo.mcp_servers.remote]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "REMOTE_MCP_TOKEN"

[profiles.yolo.shell_environment_policy]
set = { OPENAI_API_KEY = "sk-FAKE3333", PAGER = "cat" }
"""
    caps = capabilities(tmp_path, config)
    assert caps.approvals is ApprovalLevel.NEVER_ASK and caps.filesystem is FsAccess.FULL
    assert caps.network is NetAccess.FULL
    servers = {s.name: s for s in caps.mcp_servers}
    assert servers["github"].pinned is True and servers["remote"].url_host == "mcp.example.com"
    assert {s.name for s in caps.secrets} == {"GITHUB_TOKEN", "REMOTE_MCP_TOKEN", "OPENAI_API_KEY"}
    wire = json.dumps(to_dict(caps))
    assert "ghp_FAKE" not in wire and "sk-FAKE" not in wire


def test_workspace_with_network(tmp_path: Path) -> None:
    config = """
sandbox_mode = "workspace-write"
[sandbox_workspace_write]
network_access = true
writable_roots = ["/srv/build"]
"""
    caps = capabilities(tmp_path, config)
    assert caps.network is NetAccess.FULL and caps.writable_roots == ("/srv/build",)
    assert caps.approvals is ApprovalLevel.UNKNOWN  # not set: unknown, not "ask"


@pytest.mark.parametrize(
    ("cmdline", "approvals", "filesystem"),
    [
        (("codex", "--dangerously-bypass-approvals-and-sandbox"), ApprovalLevel.NEVER_ASK,
         FsAccess.FULL),
        (("codex", "--full-auto"), ApprovalLevel.AUTO_EDIT, FsAccess.WORKSPACE),
        (("codex", "-s", "read-only", "-a", "untrusted"), ApprovalLevel.ASK, FsAccess.READ_ONLY),
        (("codex", "--sandbox=danger-full-access"), ApprovalLevel.AUTO_EDIT, FsAccess.FULL),
    ],
)  # fmt: skip
def test_command_line_flags_win(
    tmp_path: Path, cmdline: tuple[str, ...], approvals: ApprovalLevel, filesystem: FsAccess
) -> None:
    proc = fake_process(9, "codex", cmdline=cmdline)
    caps = capabilities(tmp_path, None, proc)
    assert caps.approvals is approvals and caps.filesystem is filesystem


def test_missing_or_broken_config(tmp_path: Path) -> None:
    caps = capabilities(tmp_path, "not = [valid")
    assert caps.filesystem is FsAccess.UNKNOWN and caps.sources == ()
