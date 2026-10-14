import sys
from importlib.metadata import EntryPoint
from pathlib import Path
from types import ModuleType

import pytest

from openvigie.adapters import registry
from openvigie.adapters.base import ActivityBatch, Adapter, AdapterState, HostContext
from openvigie.adapters.testing import AdapterContract, fake_context, fake_process
from openvigie.model import AgentInstance, HealthState, stable_id


class DummyAdapter(Adapter):
    agent_type = "dummy"
    display_name = "Dummy"

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        found = []
        for home in ctx.homes:
            root = home / ".dummy"
            if root.is_dir():
                running = any(p.name == "dummy" for p in ctx.processes())
                found.append(
                    AgentInstance(
                        agent_id=stable_id(ctx.host.host_id, self.agent_type, str(root)),
                        host_id=ctx.host.host_id,
                        agent_type=self.agent_type,
                        name="Dummy",
                        config_root=str(root),
                        process_running=running,
                    )
                )
        return found


class CrashingAdapter(Adapter):
    agent_type = "crashy"
    display_name = "Crashy"

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        raise RuntimeError("boom with sk-ant-api03-" + "Q" * 40)

    def collect(self, ctx: HostContext, agent: AgentInstance, state: AdapterState) -> ActivityBatch:
        state.stage("cursor", {"offset": 99})
        raise RuntimeError("collect failed")


class NotAnAdapter:
    agent_type = "fake"


# Registered as an importable module so entry points can point at it.
_mod = ModuleType("ov_test_plugins")
_mod.DummyAdapter = DummyAdapter  # type: ignore[attr-defined]
_mod.CrashingAdapter = CrashingAdapter  # type: ignore[attr-defined]
_mod.NotAnAdapter = NotAnAdapter  # type: ignore[attr-defined]
sys.modules["ov_test_plugins"] = _mod

GROUP = registry.ENTRY_POINT_GROUP


def eps(*items: tuple[str, str]) -> list[EntryPoint]:
    return [EntryPoint(name, value, GROUP) for name, value in items]


def test_third_party_plugin_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry, "_entry_points", lambda: eps(("dummy", "ov_test_plugins:DummyAdapter"))
    )
    off = registry.discover(quiet=True)
    assert off.adapters == [] and off.skipped_plugins == ["dummy"]
    on = registry.discover(enable_plugins=["dummy"], quiet=True)
    assert [a.agent_type for a in on.adapters] == ["dummy"]


def test_unlisted_plugin_is_never_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry, "_entry_points", lambda: eps(("evil", "ov_does_not_exist_xyz:Adapter"))
    )
    result = registry.discover(quiet=True)
    assert result.failures == []  # import was never attempted
    assert "ov_does_not_exist_xyz" not in sys.modules


def test_bad_plugins_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry,
        "_entry_points",
        lambda: eps(
            ("fake", "ov_test_plugins:NotAnAdapter"),
            ("wrongname", "ov_test_plugins:DummyAdapter"),
            ("missing", "ov_does_not_exist_xyz:Adapter"),
        ),
    )
    result = registry.discover(enable_plugins=["fake", "wrongname", "missing"], quiet=True)
    assert result.adapters == []
    assert sorted(f.adapter for f in result.failures) == ["fake", "missing", "wrongname"]
    assert all(f.state is HealthState.ERROR for f in result.failures)


def test_builtin_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry, "_entry_points", lambda: eps(("dummy", "openvigie.adapters.nope:Dummy"))
    )
    assert registry.discover(disable=["dummy"], quiet=True).adapters == []


def test_crashing_adapter_is_isolated(tmp_path: Path) -> None:
    ctx = fake_context(homes=[tmp_path])
    agents, health = registry.run_detect(CrashingAdapter(), ctx)
    assert agents == []
    assert health.state is HealthState.ERROR
    assert health.reason is not None and "sk-ant-api03-QQQQ" not in health.reason

    state = AdapterState()
    agent = AgentInstance("a", "h", "crashy", "Crashy")
    batch, health = registry.run_collect(CrashingAdapter(), ctx, agent, state)
    assert batch is None and health.state is HealthState.ERROR
    assert state.get("cursor") is None  # staged cursor rolled back


def test_adapter_state_stage_commit() -> None:
    state = AdapterState()
    state.stage("f", {"offset": 1})
    assert state.get("f") == {"offset": 1}
    state.rollback()
    assert state.get("f") is None
    state.stage("f", {"offset": 2})
    state.commit()
    assert state.committed == {"f": {"offset": 2}}


def test_context_caches_snapshots_per_tick(tmp_path: Path) -> None:
    calls = []

    def source() -> list[object]:
        calls.append(1)
        return []

    ctx = fake_context(homes=[tmp_path])
    ctx.process_source = source  # type: ignore[assignment]
    ctx.processes()
    ctx.processes()
    assert len(calls) == 1
    ctx.refresh()
    ctx.processes()
    assert len(calls) == 2


class TestDummyContract(AdapterContract):
    adapter_cls = DummyAdapter

    def scenarios(self, tmp_path: Path) -> dict[str, HostContext]:
        home = tmp_path / "home"
        (home / ".dummy").mkdir(parents=True)
        return {
            "installed": fake_context(homes=[home]),
            "running": fake_context(homes=[home], processes=[fake_process(10, "dummy")]),
        }
