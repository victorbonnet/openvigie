"""Starter OpenShell policies (FR-11), golden-tested on the adapters' recorded fixtures."""

import os
import re
import shutil
from pathlib import Path

import pytest

from openvigie.adapters.base import Adapter
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.testing import fake_context
from openvigie.model import AgentInstance, Capabilities, McpServer
from openvigie.openshell.starter import StarterPolicy, generate

TESTS = Path(__file__).parents[1]
GOLDEN = Path(__file__).parent / "starter"
HOMES = {
    "claude-code": TESTS / "adapters/claude_code/fixtures/capabilities/home",
    "codex": TESTS / "adapters/codex/fixtures/home",
}
ADAPTERS: dict[str, type[Adapter]] = {"claude-code": ClaudeCodeAdapter, "codex": CodexAdapter}
# Every line the emitter writes has one of these shapes; anything else means a value broke out
LINE = re.compile(
    r"^(#.*|version: 1|filesystem_policy:|landlock:|network_policies:( \{\})?(  #.*)?"
    r'|  include_workdir: (true|false)(  #.*)?|  (read_only|read_write):|    - "[^"\n]*"'
    r'|  compatibility: "best_effort"|  # .*|  [a-z0-9_]+:|    endpoints:|    binaries:( \[\].*)?'
    r'|      - host: "[^"\n]*"|        port: 443|      - path: "[^"\n]*")$'
)


def from_fixture(agent_type: str, tmp_path: Path) -> tuple[AgentInstance, Capabilities | None]:
    home = tmp_path / "home"
    shutil.copytree(HOMES[agent_type], home)
    state = home / ".claude.json"
    if state.exists():
        state.write_text(state.read_text().replace("{HOME}", str(home)))
    ctx = fake_context(homes=[home])
    adapter = ADAPTERS[agent_type]()
    (agent,) = adapter.detect(ctx)
    return agent, adapter.capabilities(ctx, agent)


def check_shape(policy: StarterPolicy) -> None:
    bad = [line for line in policy.yaml.splitlines() if not LINE.match(line)]
    assert bad == []


@pytest.mark.parametrize("agent_type", sorted(HOMES))
def test_golden(agent_type: str, tmp_path: Path) -> None:
    policy = generate(*from_fixture(agent_type, tmp_path))
    check_shape(policy)
    golden = GOLDEN / f"{agent_type}.yaml"
    if os.environ.get("OPENVIGIE_UPDATE_GOLDEN"):
        golden.write_text(policy.yaml)
    assert policy.yaml == golden.read_text()


def test_claude_code_rules_follow_its_mcp_servers(tmp_path: Path) -> None:
    policy = generate(*from_fixture("claude-code", tmp_path))
    hosts = {r.name: r.hosts for r in policy.rules}
    assert hosts["model_api_1"] == ("api.anthropic.com",)
    assert hosts["mcp_docs"] == ("mcp.example.com",)  # remote MCP server
    assert hosts["mcp_github_packages"] == ("registry.npmjs.org",)  # npx
    assert hosts["mcp_db_packages"] == ("pypi.org", "files.pythonhosted.org")  # uvx
    assert "~/shared on the host" in policy.yaml  # a host path becomes a comment, not a rule


def test_hostile_names_cannot_change_the_structure() -> None:
    agent = AgentInstance("a1", "h", "claude-code", 'evil"\nnetwork_policies: {}')
    caps = Capabilities("a1", network_allowlist=("ok.example.com", 'x.com"\n  open:', "localhost"),
                        mcp_servers=(McpServer('a\n  b: "c"', "http", None, "mcp.example.com"),
                                     McpServer("tool", "stdio", "mystery-binary")))  # fmt: skip
    policy = generate(agent, caps)
    check_shape(policy)
    keys = [line for line in policy.yaml.splitlines() if not line.lstrip().startswith("#")]
    assert sum(line.startswith("network_policies") for line in keys) == 1
    assert [r.name for r in policy.rules][-2:] == ["agent_allowlist", "mcp_a_b_c"]
    assert any("aren't host names" in n for n in policy.notes)
    assert any("mystery-binary" in n for n in policy.notes)


def test_unknown_agent_type_gets_no_network_and_a_note() -> None:
    policy = generate(AgentInstance("a1", "h", "hermes", "Hermes"), None)
    assert "network_policies: {}" in policy.yaml and policy.rules == ()
    assert policy.notes == ("openvigie doesn't know which hosts hermes needs: add them.",)
