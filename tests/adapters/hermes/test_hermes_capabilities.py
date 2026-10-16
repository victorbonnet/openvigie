import shutil
from pathlib import Path

import yaml

from openvigie.adapters.hermes import HermesAdapter
from openvigie.adapters.hermes.adapter import platforms_by_profile
from openvigie.adapters.testing import fake_context
from openvigie.model import ApprovalLevel, Capabilities, Channel, FsAccess, NetAccess

FIXTURE = Path(__file__).parent / "fixtures" / "0.21.7" / "home" / ".hermes"


def capabilities(tmp_path: Path, profile: str = "default", **config: object) -> Capabilities:
    home = tmp_path / "home"
    shutil.copytree(FIXTURE, home / ".hermes")
    target = home / ".hermes"
    if profile != "default":
        target = target / "profiles" / profile
        target.mkdir(parents=True)
        shutil.copy(FIXTURE / "state.db", target / "state.db")
        (target / "config.yaml").write_text(yaml.safe_dump(config))
    elif config:
        data = yaml.safe_load((target / "config.yaml").read_text())
        (target / "config.yaml").write_text(yaml.safe_dump({**data, **config}))
    ctx = fake_context(homes=[home])
    adapter = HermesAdapter()
    agents = {a.name.rpartition(" · ")[2] or "default": a for a in adapter.detect(ctx)}
    agent = agents[profile if profile in agents else "Hermes"]
    caps = adapter.capabilities(ctx, agent)
    assert caps is not None
    return caps


def test_recorded_profile(tmp_path: Path) -> None:
    caps = capabilities(tmp_path)
    assert caps.approvals is ApprovalLevel.AUTO_EDIT and caps.approval_mode == "manual"
    assert caps.filesystem is FsAccess.WORKSPACE  # docker backend
    assert caps.network is NetAccess.UNKNOWN
    assert caps.shell is True
    assert "hermes-cli" in caps.tools_allowed and "terminal" in caps.tools_allowed
    assert caps.channels == (Channel("telegram"),)  # no .env recorded: who may write is unknown
    assert caps.sources == ("~/.hermes/config.yaml",)


def test_profile_defaults_and_its_platforms(tmp_path: Path) -> None:
    caps = capabilities(tmp_path, "profile-1")
    assert caps.approvals is ApprovalLevel.AUTO_EDIT and caps.approval_mode == "smart"
    assert caps.filesystem is FsAccess.FULL and caps.network is NetAccess.FULL  # local backend
    assert caps.shell is True and caps.tools_allowed == ("hermes-cli",)
    assert caps.channels == (Channel("telegram"),)
    assert caps.sources == ("~/.hermes/profiles/profile-1/config.yaml",)


def test_approvals_off_and_terminal_disabled(tmp_path: Path) -> None:
    caps = capabilities(tmp_path, "profile-1", approvals={"mode": False},
                        agent={"disabled_toolsets": ["terminal"]})  # fmt: skip
    assert caps.approvals is ApprovalLevel.NEVER_ASK and caps.approval_mode == "off"
    assert caps.shell is False and caps.tools_denied == ("terminal",)


def test_mcp_servers_and_secret_names_only(tmp_path: Path) -> None:
    caps = capabilities(
        tmp_path, "profile-1",
        mcp_servers={"github": {"command": "npx", "args": ["-y", "@mcp/server-github"],
                                "env": {"GITHUB_TOKEN": "ghp_never_read"}}},
        terminal={"backend": "local", "env_passthrough": ["OPENAI_API_KEY", "PATH"]},
        delegation={"api_key": "sk-never-read", "model": "x"},
    )  # fmt: skip
    (server,) = caps.mcp_servers
    assert server.name == "github" and server.pinned is False
    assert {s.name for s in caps.secrets} == {
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "delegation.api_key",
    }
    assert "never" not in repr(caps)


def test_skills_are_listed_by_name(tmp_path: Path) -> None:
    home = tmp_path / "home" / ".hermes"
    shutil.copytree(FIXTURE, home)
    for path in ("research/arxiv", "github/pr-review"):
        (home / "skills" / path).mkdir(parents=True)
        (home / "skills" / path / "SKILL.md").write_text("---\nname: x\n---\n")
    ctx = fake_context(homes=[tmp_path / "home"])
    adapter = HermesAdapter()
    (agent,) = adapter.detect(ctx)
    caps = adapter.capabilities(ctx, agent)
    assert caps is not None and caps.skills == ("arxiv", "pr-review")


def test_broken_config_reports_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home" / ".hermes"
    shutil.copytree(FIXTURE, home)
    (home / "config.yaml").write_text("model: [unclosed\n")
    ctx = fake_context(homes=[tmp_path / "home"])
    adapter = HermesAdapter()
    (agent,) = adapter.detect(ctx)
    assert adapter.capabilities(ctx, agent) is None


def test_platforms_by_profile_skips_odd_keys() -> None:
    keys = ["telegram", "alice:discord", "alice:telegram", "../x:telegram", "bob:Bad Name", "a:b:c"]
    assert platforms_by_profile(keys) == {
        "default": frozenset({"telegram"}),
        "alice": frozenset({"discord", "telegram"}),
    }
