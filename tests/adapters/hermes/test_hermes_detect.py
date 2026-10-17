import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openvigie.adapters.base import HostContext, ProcessInfo
from openvigie.adapters.common import owner_name
from openvigie.adapters.hermes import HermesAdapter
from openvigie.adapters.hermes import adapter as hermes
from openvigie.adapters.hermes.adapter import is_loose_hermes
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
    (tmp_path / "home" / ".hermes").mkdir(parents=True, exist_ok=True)
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


def test_loose_process_matching() -> None:
    pip_install = fake_process(5, "hermes", ("/usr/bin/python3", "/usr/local/bin/hermes"),
                               exe="/usr/bin/python3.12")  # fmt: skip
    framework = "/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app"
    macos = fake_process(6, "hermes", ("Python", "/opt/homebrew/bin/hermes"),
                         exe=f"{framework}/Contents/MacOS/Python")  # fmt: skip
    module = fake_process(7, "python3", ("python3", "-m", "hermes_cli", "gateway", "run"))
    js_engine = fake_process(8, "hermes", ("hermes", "app.js"), exe="/usr/local/bin/hermes")
    assert is_loose_hermes(pip_install)
    assert is_loose_hermes(macos)
    assert is_loose_hermes(module)
    assert not is_loose_hermes(js_engine)  # Meta's Hermes JS engine shares the name
    assert not is_loose_hermes(fake_process(9, "python3", ("python3", "app.py")))


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


def add_profile(home: Path, name: str) -> Path:
    profile = home / ".hermes" / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    for file in ("config.yaml", "state.db"):
        shutil.copy(FIXTURE / file, profile / file)
    return profile


def test_one_agent_per_profile(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    add_profile(home, "profile-1")
    add_profile(home, "profile-2")
    ctx = fake_context(homes=[home], processes=[gateway(home)],
                       sandboxed={1302: (SandboxState.NO, None)})  # fmt: skip
    adapter = HermesAdapter()
    agents = {a.name: a for a in adapter.detect(ctx)}
    assert list(agents) == ["Hermes · default", "Hermes · profile-1", "Hermes · profile-2"]
    served = agents["Hermes · profile-1"]
    assert served.config_root == "~/.hermes/profiles/profile-1"
    assert served.version == "0.21.7"
    assert served.process_running is True
    assert served.sandboxed is SandboxState.NO
    assert served.last_activity_at is not None
    assert adapter.processes(served.agent_id) == ()
    assert [p.pid for p in adapter.processes(agents["Hermes · default"].agent_id)] == [1302]


def test_default_profile_keeps_its_id(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (before,) = HermesAdapter().detect(fake_context(homes=[home]))
    add_profile(home, "profile-1")
    after = HermesAdapter().detect(fake_context(homes=[home]))
    assert after[0].agent_id == before.agent_id
    assert after[1].agent_id != before.agent_id


def test_profile_not_served_is_stopped(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    add_profile(home, "unserved")
    ctx = fake_context(homes=[home], processes=[gateway(home)])
    agents = {a.name: a for a in HermesAdapter().detect(ctx)}
    assert agents["Hermes · default"].process_running is True
    assert agents["Hermes · unserved"].process_running is False
    assert agents["Hermes · unserved"].sandboxed is SandboxState.UNKNOWN


def test_profile_with_its_own_gateway(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    add_profile(home, "solo")
    python = f"{home}/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3"
    solo = fake_process(400, "hermes", (python, "-I", "hermes", "-p", "solo", "gateway", "run"),
                        exe=python)  # fmt: skip
    adapter = HermesAdapter()
    agents = {a.name: a for a in adapter.detect(fake_context(homes=[home], processes=[solo]))}
    assert agents["Hermes · solo"].process_running is True
    assert [p.pid for p in adapter.processes(agents["Hermes · solo"].agent_id)] == [400]
    assert agents["Hermes · default"].process_running is False


def test_odd_profile_folders_are_skipped(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    profiles = home / ".hermes" / "profiles"
    (profiles / "empty").mkdir(parents=True, exist_ok=True)
    (profiles / ".hidden").mkdir()
    (profiles / "linked").symlink_to(FIXTURE)
    (profiles / "file").write_text("")
    (agent,) = HermesAdapter().detect(fake_context(homes=[home]))
    assert agent.name == "Hermes"


def test_broken_profile_config_names_the_profile(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (add_profile(home, "profile-1") / "config.yaml").write_text("model: [unclosed\n")
    adapter = HermesAdapter()
    assert len(adapter.detect(fake_context(homes=[home]))) == 2
    assert adapter.health().reason == "profile profile-1: config.yaml is not valid YAML"


def test_deeply_nested_config_degrades_only_its_profile(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (add_profile(home, "profile-1") / "config.yaml").write_text("[" * 20_000)
    adapter = HermesAdapter()
    assert len(adapter.detect(fake_context(homes=[home]))) == 2
    assert adapter.health().reason == "profile profile-1: config.yaml is not valid YAML"


def test_unchanged_config_is_parsed_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = make_home(tmp_path)
    calls: list[Path] = []
    real = hermes.config_problem

    def spy(config: Path) -> str | None:
        calls.append(config)
        return real(config)

    monkeypatch.setattr(hermes, "config_problem", spy)
    adapter = HermesAdapter()
    adapter.detect(fake_context(homes=[home]))
    adapter.detect(fake_context(homes=[home]))
    assert len(calls) == 1


def solo_gateway(home: Path, pid: int = 400) -> ProcessInfo:
    python = f"{home}/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3"
    return fake_process(pid, "hermes", (python, "-I", "hermes", "-p", "solo", "gateway", "run"),
                        exe=python)  # fmt: skip


def test_helpers_follow_their_profile_gateway(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    add_profile(home, "solo")
    python = f"{home}/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3"
    script = f"{home}/.hermes/hermes-agent/tools/mcp_death_supervisor.py"
    supervisor = fake_process(410, "python3", (python, script, "--parent-pgid", "400"),
                              exe=python, ppid=400)  # fmt: skip
    adapter = HermesAdapter()
    ctx = fake_context(homes=[home], processes=[solo_gateway(home), supervisor])
    agents = {a.name: a for a in adapter.detect(ctx)}
    assert [p.pid for p in adapter.processes(agents["Hermes · solo"].agent_id)] == [400, 410]
    assert agents["Hermes · default"].process_running is False
    assert adapter.processes(agents["Hermes · default"].agent_id) == ()


def test_chat_session_does_not_make_served_profiles_run(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    add_profile(home, "profile-1")
    python = f"{home}/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3"
    chat = fake_process(500, "hermes", (python, "-I", "hermes", "chat"), exe=python)
    ctx = fake_context(homes=[home], processes=[chat])
    agents = {a.name: a for a in HermesAdapter().detect(ctx)}
    assert agents["Hermes · default"].process_running is True
    assert agents["Hermes · profile-1"].process_running is False


def test_removed_profile_forgets_its_processes(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    profile = add_profile(home, "solo")
    ctx = fake_context(homes=[home], processes=[solo_gateway(home)])
    adapter = HermesAdapter()
    solo_id = {a.name: a for a in adapter.detect(ctx)}["Hermes · solo"].agent_id
    shutil.rmtree(profile)
    adapter.detect(ctx)
    assert adapter.processes(solo_id) == ()
    assert solo_id not in adapter.roots


def test_loose_process_with_two_candidate_homes_is_left_out(tmp_path: Path) -> None:
    alice, bob = make_home(tmp_path, "alice"), make_home(tmp_path, "bob")
    pip_gateway = fake_process(600, "hermes", ("/usr/bin/python3", "/usr/local/bin/hermes",
                                               "gateway", "run"),
                               exe="/usr/bin/python3", username=owner_name(tmp_path))  # fmt: skip
    agents = HermesAdapter().detect(fake_context(homes=[alice, bob], processes=[pip_gateway]))
    assert [a.process_running for a in agents] == [False, False]


def test_home_set_to_a_profile_is_reported_once(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    profile = add_profile(home, "work")
    agents = HermesAdapter().detect(fake_context(homes=[home], env={"HERMES_HOME": str(profile)}))
    assert [a.name for a in agents] == ["Hermes · default", "Hermes · work"]


def test_nested_home_gets_its_own_processes(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    inner = home / ".hermes" / "inner"
    shutil.copytree(FIXTURE, inner)
    python = f"{inner}/tools/python-3.14.7+20260901-linux-arm64/bin/python3"
    inner_gateway = fake_process(700, "hermes", (python, "gateway", "run"), exe=python)
    adapter = HermesAdapter()
    ctx = fake_context(homes=[home], env={"HERMES_HOME": str(inner)}, processes=[inner_gateway])
    agents = {adapter.roots[a.agent_id]: a for a in adapter.detect(ctx)}
    assert agents[inner].process_running is True
    assert agents[home / ".hermes"].process_running is False


def test_processes_match_behind_a_symlinked_home(tmp_path: Path) -> None:
    real = make_home(tmp_path, "real")
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    adapter = HermesAdapter()
    (agent,) = adapter.detect(fake_context(homes=[linked], processes=[helper(real.resolve())]))
    assert agent.config_root == "~/.hermes"
    assert [p.pid for p in adapter.processes(agent.agent_id)] == [1310]


def test_junk_in_profiles_does_not_hide_real_ones(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    profiles = home / ".hermes" / "profiles"
    for i in range(150):
        (profiles / f".cache-{i:03}").mkdir(parents=True, exist_ok=True)
    add_profile(home, "zeta")
    agents = HermesAdapter().detect(fake_context(homes=[home]))
    assert [a.name for a in agents] == ["Hermes · default", "Hermes · zeta"]


def test_long_profile_listing_is_cut_and_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hermes, "MAX_PROFILE_ENTRIES", 3)
    home = make_home(tmp_path)
    shutil.rmtree(home / ".hermes" / "profiles")  # only the profiles below
    for i in range(5):
        add_profile(home, f"profile-{i}")
    adapter = HermesAdapter()
    assert len(adapter.detect(fake_context(homes=[home]))) == 4
    assert adapter.health().reason == "~/.hermes: more profiles than read, some are not shown"


def test_unexpected_gateway_state_names_the_home(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    (home / ".hermes" / "gateway_state.json").write_text('{"code_version": 7}')
    adapter = HermesAdapter()
    (agent,) = adapter.detect(fake_context(homes=[home]))
    assert agent.version is None
    assert adapter.health().reason == "~/.hermes/gateway_state.json could not be read"
