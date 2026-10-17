import json
import shutil
from pathlib import Path
from typing import Any

from openvigie.adapters import caps as caps_helpers
from openvigie.adapters.openclaw import OpenClawAdapter
from openvigie.adapters.testing import fake_context
from openvigie.model import ApprovalLevel, Capabilities, Channel, FsAccess, NetAccess, to_dict
from openvigie.risk import evaluate, facts, load_rules

FIXTURE = Path(__file__).parent / "fixtures" / "2026.9.4" / "home" / ".openclaw"


def known(caps: Capabilities) -> tuple[Channel, ...]:
    assert caps.channels is not None
    return caps.channels


def capabilities(tmp_path: Path, **changes: Any) -> dict[str, Capabilities]:
    root = tmp_path / "home" / ".openclaw"
    shutil.copytree(FIXTURE, root)
    if changes:
        config = json.loads((root / "openclaw.json").read_text())
        (root / "openclaw.json").write_text(json.dumps({**config, **changes}))
    ctx = fake_context(homes=[tmp_path / "home"])
    adapter = OpenClawAdapter()
    found = {}
    for agent in adapter.detect(ctx):
        caps = adapter.capabilities(ctx, agent)
        assert caps is not None
        found[agent.name.split(" · ")[1]] = caps
    return found


def recorded_channels() -> dict[str, Any]:
    channels: dict[str, Any] = json.loads((FIXTURE / "openclaw.json").read_text())["channels"]
    return channels


def test_recorded_gateway(tmp_path: Path) -> None:
    caps = capabilities(tmp_path)
    main = caps["main"]
    assert main.shell is True  # "coding" tool profile
    assert main.filesystem is FsAccess.FULL and main.network is NetAccess.FULL  # no sandbox
    assert main.approvals is ApprovalLevel.UNKNOWN  # exec approvals live outside openclaw.json
    assert [h.command for h in main.hooks] == ["command-logger", "session-memory"]
    assert main.sources == ("~/.openclaw/openclaw.json",)
    assert {s.name for s in main.secrets} == {"channels.telegram.accounts.default.botToken",
                                              "plugins.brave.webSearch.apiKey"}  # fmt: skip


def test_each_agent_gets_its_own_bot_and_groups(tmp_path: Path) -> None:
    caps = capabilities(tmp_path)
    assert caps["main"].channels == (Channel("telegram", "pairing", False, None, False),)
    assert caps["lutra"].channels == (Channel("telegram:lutra", "pairing", False, None, False),)
    # vulpes: one allowlisted person in DMs, and a bound group open to anyone
    assert caps["vulpes"].channels == (Channel("telegram:vulpes", "allowlist", False, 1, True),)


def test_unbound_agent_has_no_channel(tmp_path: Path) -> None:
    config = json.loads((FIXTURE / "openclaw.json").read_text())
    config["agents"]["entries"]["test"] = {"workspace": "/home/user/.openclaw/workspace-test"}
    assert capabilities(tmp_path, agents=config["agents"])["test"].channels == ()


def test_chat_ids_and_tokens_never_leave_the_collector(tmp_path: Path) -> None:
    channels = recorded_channels()
    channels["telegram"]["accounts"]["lutra"] |= {
        "botToken": "123456789:AAFakeTokenForTestsOnly0123456789abcd",
        "dmPolicy": "allowlist",
        "allowFrom": ["tg:555000111", "+15550001111"],
    }
    sent = json.dumps([to_dict(c) for c in capabilities(tmp_path, channels=channels).values()])
    for value in ("-100000004", "100000162", "555000111", "+15550001111", "AAFakeToken"):
        assert value not in sent


def test_open_group_with_shell_is_high_risk(tmp_path: Path) -> None:
    caps = capabilities(tmp_path)
    adapter = OpenClawAdapter()
    (vulpes,) = [a for a in adapter.detect(fake_context(homes=[tmp_path / "home"]))
                 if a.name.endswith("vulpes")]  # fmt: skip
    flags = evaluate(load_rules(tmp_path / "none.toml"), facts(vulpes, caps["vulpes"], "linux"))
    assert "others-instruct-broad-access" in [f.rule_id for f in flags]


def test_dm_policies(tmp_path: Path) -> None:
    channels = recorded_channels()
    accounts = channels["telegram"]["accounts"]
    accounts["default"] |= {"dmPolicy": "open"}
    accounts["lutra"] |= {"dmPolicy": "allowlist", "allowFrom": ["tg:1", "tg:2", "tg:1"],
                          "groupPolicy": "disabled"}  # fmt: skip
    caps = capabilities(tmp_path, channels=channels)
    assert known(caps["main"])[0].open_dms is True
    assert caps["lutra"].channels == (Channel("telegram:lutra", "allowlist", False, 2, False),)
    assert "tg:1" not in repr(caps["lutra"])


def test_owner_only_dm_is_not_flagged(tmp_path: Path) -> None:
    channels = recorded_channels()
    channels["telegram"]["accounts"]["lutra"] |= {"dmPolicy": "allowlist", "allowFrom": ["tg:1"],
                                                  "groupPolicy": "disabled"}  # fmt: skip
    caps = capabilities(tmp_path, channels=channels)
    adapter = OpenClawAdapter()
    (lutra,) = [a for a in adapter.detect(fake_context(homes=[tmp_path / "home"]))
                if a.name.endswith("lutra")]  # fmt: skip
    flags = evaluate(load_rules(tmp_path / "none.toml"), facts(lutra, caps["lutra"], "linux"))
    assert not [f for f in flags if f.rule_id.startswith("others-instruct")]


def test_wildcard_allowlist_means_anyone(tmp_path: Path) -> None:
    channels = recorded_channels()
    channels["telegram"]["accounts"]["lutra"] |= {"dmPolicy": "allowlist", "allowFrom": ["*"]}
    (channel,) = known(capabilities(tmp_path, channels=channels)["lutra"])
    assert channel.open_dms is True and channel.allowed_users is None


def test_exec_settings_and_sandbox(tmp_path: Path) -> None:
    allowlist = {"exec": {"security": "allowlist", "ask": "on-miss"}}
    caps = capabilities(
        tmp_path,
        tools={"profile": "coding", "exec": {"security": "full", "ask": "off"}},
        agents={"defaults": {"sandbox": {"mode": "all"}},
                "entries": {"main": {"tools": allowlist},
                            "lutra": {"tools": {"deny": ["exec"]}}, "vulpes": {}}},
    )  # fmt: skip
    assert caps["vulpes"].approvals is ApprovalLevel.NEVER_ASK
    assert caps["vulpes"].approval_mode == "exec full, ask off"
    assert caps["main"].approvals is ApprovalLevel.AUTO_EDIT
    assert caps["lutra"].shell is False and caps["lutra"].tools_denied == ("exec",)
    assert caps["main"].filesystem is FsAccess.WORKSPACE
    assert caps["main"].network is NetAccess.UNKNOWN


def test_messaging_profile_has_no_shell(tmp_path: Path) -> None:
    assert capabilities(tmp_path, tools={"profile": "messaging"})["main"].shell is False
    assert capabilities(tmp_path / "x", tools={})["main"].shell is None


def test_mcp_servers(tmp_path: Path) -> None:
    servers = {"docs": {"url": "https://mcp.example.com/mcp", "transport": "streamable-http",
                        "headers": {"Authorization": "Bearer never-read"}},
               "off": {"command": "npx", "args": ["x"], "enabled": False}}  # fmt: skip
    main = capabilities(tmp_path, mcp={"servers": servers})["main"]
    (docs,) = main.mcp_servers
    assert docs.url_host == "mcp.example.com" and docs.secret_names == ("Authorization",)
    assert "never-read" not in repr(main)


def test_json5_config_reports_nothing(tmp_path: Path) -> None:
    root = tmp_path / "home" / ".openclaw"
    shutil.copytree(FIXTURE, root)
    (root / "openclaw.json").write_text("{ // json5\n agents: {} }")
    ctx = fake_context(homes=[tmp_path / "home"])
    adapter = OpenClawAdapter()
    (agent, *_) = adapter.detect(ctx)
    assert adapter.capabilities(ctx, agent) is None


def test_unknown_policies_leave_reach_unknown(tmp_path: Path) -> None:
    channels = recorded_channels()
    accounts = channels["telegram"]["accounts"]
    accounts["lutra"] |= {"dmPolicy": "allowAll", "groupPolicy": "everyone"}
    accounts["default"] |= {"dmPolicy": "open"}
    caps = capabilities(tmp_path, channels=channels)
    assert caps["lutra"].channels == (Channel("telegram:lutra", "allowAll", None, None, None),)
    (main,) = known(caps["main"])
    assert main.open_dms is True and main.allowed_users is None


def test_binding_without_an_account_uses_the_default_one(tmp_path: Path) -> None:
    config = json.loads((FIXTURE / "openclaw.json").read_text())
    bindings = [
        {"agentId": "lutra", "match": {"channel": "telegram"}},
        {"agentId": "main", "match": {"channel": "telegram", "accountId": "*"}},
    ]
    caps = capabilities(tmp_path, bindings=bindings)
    assert [c.platform for c in known(caps["lutra"])] == ["telegram"]
    assert {c.platform for c in known(caps["main"])} == {
        "telegram", "telegram:lutra", "telegram:vulpes"}  # fmt: skip
    assert config["bindings"] != bindings


def test_group_senders_count_as_people(tmp_path: Path) -> None:
    channels = recorded_channels()
    channels["telegram"]["accounts"]["lutra"] |= {"dmPolicy": "allowlist", "allowFrom": ["tg:1"]}
    channels["telegram"]["groups"]["-1009"] = {"allowFrom": ["tg:2", "tg:3"]}
    config = json.loads((FIXTURE / "openclaw.json").read_text())
    bindings = [
        *config["bindings"],
        {
            "agentId": "lutra",
            "match": {
                "channel": "telegram",
                "accountId": "lutra",
                "peer": {"kind": "group", "id": "-1009"},
            },
        },
    ]
    (channel,) = known(capabilities(tmp_path, channels=channels, bindings=bindings)["lutra"])
    assert channel.allowed_users == 3


def test_channel_level_bot_token_is_a_secret(tmp_path: Path) -> None:
    channels = {"telegram": {"botToken": "123:never-read", "dmPolicy": "pairing"}}
    bindings = [{"agentId": "main", "match": {"channel": "telegram"}}]
    main = capabilities(tmp_path, channels=channels, bindings=bindings)["main"]
    assert "channels.telegram.botToken" in {s.name for s in main.secrets}
    assert "never-read" not in repr(main)


def test_an_agent_can_only_narrow_the_tool_policy(tmp_path: Path) -> None:
    config = json.loads((FIXTURE / "openclaw.json").read_text())
    config["agents"]["entries"]["main"]["tools"] = {"deny": ["browser"], "allow": ["read", "x"]}
    tools = {"profile": "coding", "deny": ["exec"], "allow": ["read", "write"]}
    main = capabilities(tmp_path, tools=tools, agents=config["agents"])["main"]
    assert main.shell is False
    assert main.tools_denied == ("exec", "browser") and main.tools_allowed == ("read",)


def test_many_secrets_are_capped(tmp_path: Path) -> None:
    plugins = {"entries": {f"p{i}": {"config": {"apiKey": "k"}} for i in range(1100)}}
    assert len(capabilities(tmp_path, plugins=plugins)["main"].secrets) == caps_helpers.MAX_ITEMS
