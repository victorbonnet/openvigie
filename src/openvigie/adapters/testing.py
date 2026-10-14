"""Test kit for adapters: a fake host builder and a reusable contract suite.

Usable by third-party adapter authors too (no pytest import here)::

    class TestMyAgent(AdapterContract):
        adapter_cls = MyAgentAdapter

        def scenarios(self, tmp_path):  # each one is a fake host to run the contract on
            return {"installed": fake_context(homes=[tmp_path / "home"])}
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from openvigie.adapters.base import (
    AGENT_TYPE_RE,
    Adapter,
    AdapterState,
    HostContext,
    ListeningPort,
    ProcessInfo,
)
from openvigie.model import Host, SandboxState, from_dict, to_dict

FIXED_NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
TEST_HOST = Host("host-test", "testhost", "linux", "aarch64", "0.0.0")


def fake_process(
    pid: int,
    name: str,
    cmdline: Iterable[str] = (),
    *,
    exe: str = "",
    ppid: int = 1,
    username: str | None = "user",
    create_time: float = 0.0,
) -> ProcessInfo:
    args = tuple(cmdline) or (name,)
    return ProcessInfo(pid, ppid, name, exe or f"/usr/bin/{name}", username, create_time, args)


def fake_context(
    homes: Iterable[Path],
    *,
    processes: Iterable[ProcessInfo] = (),
    ports: Iterable[ListeningPort] | None = None,
    env: Mapping[str, str] | None = None,
    sandboxed: Mapping[int, tuple[SandboxState, str | None]] | None = None,
    now: datetime = FIXED_NOW,
    host: Host = TEST_HOST,
) -> HostContext:
    procs = tuple(processes)
    port_list = None if ports is None else tuple(ports)
    sandbox = dict(sandboxed or {})
    return HostContext(
        host=host,
        homes=tuple(homes),
        environ=dict(env or {}),
        process_source=lambda: procs,
        port_source=lambda: port_list,
        sandbox_probe=lambda pid: sandbox.get(pid, (SandboxState.UNKNOWN, None)),
        clock=lambda: now,
    )


class AdapterContract:
    """Checks every adapter must pass on each scenario its test class declares."""

    adapter_cls: type[Adapter]

    def scenarios(self, tmp_path: Path) -> dict[str, HostContext]:
        return {}

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def test_contract_metadata(self) -> None:
        cls = self.adapter_cls
        self._require(bool(AGENT_TYPE_RE.match(cls.agent_type)), "agent_type must be a slug")
        self._require(bool(cls.display_name), "display_name is required")

    def test_contract_empty_host(self, tmp_path: Path) -> None:
        ctx = fake_context(homes=[tmp_path / "empty-home"])
        self._require(self.adapter_cls().detect(ctx) == [], "empty host must detect nothing")

    def test_contract_scenarios(self, tmp_path: Path) -> None:
        for name, ctx in self.scenarios(tmp_path).items():
            adapter = self.adapter_cls()
            first = adapter.detect(ctx)
            ctx.refresh()
            second = adapter.detect(ctx)
            ids = [a.agent_id for a in first]
            self._require(ids == [a.agent_id for a in second], f"{name}: ids must be stable")
            self._require(len(ids) == len(set(ids)), f"{name}: duplicate agent ids")
            for agent in first:
                self._require(agent.host_id == ctx.host.host_id, f"{name}: wrong host_id")
                self._require(agent.agent_type == self.adapter_cls.agent_type, f"{name}: type")
                self._require(from_dict(type(agent), to_dict(agent)) == agent, f"{name}: codec")
                batch = adapter.collect(ctx, agent, AdapterState())
                for record in batch.usage:
                    self._require(record.agent_id == agent.agent_id, f"{name}: usage agent_id")
                    self._require(
                        from_dict(type(record), to_dict(record)) == record, f"{name}: codec"
                    )
