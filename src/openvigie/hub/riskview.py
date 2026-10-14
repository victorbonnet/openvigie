"""Risk of the agents the hub knows about, computed on read from their capabilities (FR-9)."""

from __future__ import annotations

from dataclasses import dataclass

from openvigie.hub.store.capabilities import CapabilityStore
from openvigie.hub.store.db import Database
from openvigie.hub.store.inventory import AgentRow, Inventory
from openvigie.risk import Flag, RuleSet, evaluate, facts, level


@dataclass(frozen=True)
class AgentRisk:
    level: str  # high | medium | low | none | unknown
    flags: list[Flag]


def agent_risks(db: Database, rules: RuleSet, rows: list[AgentRow]) -> dict[str, AgentRisk]:
    store = CapabilityStore(db)
    host_os = {h.host.host_id: h.host.os for h in Inventory(db).hosts()}
    out = {}
    for row in rows:
        caps = store.get(row.agent.agent_id)
        flags = evaluate(rules, facts(row.agent, caps, host_os.get(row.agent.host_id)))
        out[row.agent.agent_id] = AgentRisk(level(flags, caps is not None), flags)
    return out
