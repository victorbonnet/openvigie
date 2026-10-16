"""Usage and cost aggregation (FR-3, FR-4), shared by the UI and ``/api/v1/usage``.

SQLite sums tokens per (agent, model, provider, hour, quality, prompt-size tier); Python then
prices each group (cost is linear in tokens within a tier) and folds groups into the requested
dimensions, bucketing days in the display timezone. Every row carries its coverage: how many
records were exact, estimated or unknown, and how many tokens had no price. Unknown is never
summed as zero; a row with any unknown or unpriced part reports its cost as a lower bound.

So does a row that usage not read yet could still add to (FR-3): while a collector is still
reading an agent's history, that agent's rows reaching past what has been read are incomplete.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any

from openvigie.hub.store.db import Database, parse_ts, ts
from openvigie.model import UsageQuality, UsageRecord
from openvigie.pricing import PriceTable, compute_cost

DIMENSIONS = ("agent", "host", "model", "day", "session")
TIER_PROMPT_TOKENS = 100_000  # the only size-dependent price today (Claude Haiku 5.5)


@dataclass
class UsageRow:
    key: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    records: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    exact: int = 0
    estimated: int = 0
    unknown: int = 0
    unpriced_tokens: int = 0
    cost: Decimal = Decimal(0)
    # models whose bundled price is marked unverified: their cost is shown, with a caveat
    unverified_models: set[str] = field(default_factory=set)
    # False: an agent's history is still being read, so usage may be missing from this row
    history_complete: bool = True

    @property
    def tokens(self) -> int:
        return (self.input_tokens + self.output_tokens + self.cache_read_tokens
                + self.cache_write_tokens)  # fmt: skip

    @property
    def cost_complete(self) -> bool:
        """False when part of the usage could not be priced or is not read yet: ``cost`` is then
        a lower bound."""
        return self.unknown == 0 and self.unpriced_tokens == 0 and self.history_complete

    @property
    def cost_usd(self) -> str:
        """The cost as a decimal string, to the micro-dollar."""
        return str(self.cost.quantize(Decimal("0.000001")))


@dataclass(frozen=True)
class Reading:
    """An agent whose collector is still reading its history: usage after ``read_until`` (or
    any usage, when that is unknown) may be missing."""

    agent_id: str
    host_id: str
    label: str
    read_until: datetime | None
    bytes_left: int | None


@dataclass
class UsageReport:
    rows: list[UsageRow]
    total: UsageRow
    start: datetime
    end: datetime
    reading: list[Reading] = field(default_factory=list)  # agents that make the report partial


def _local_day(hour: str, tz: tzinfo) -> date:
    return datetime.fromisoformat(hour + ":00:00+00:00").astimezone(tz).date()


def usage_report(
    db: Database,
    prices: PriceTable,
    *,
    start: datetime,
    end: datetime,
    group_by: Iterable[str] = (),
    tz: tzinfo = UTC,
    agent_id: str | None = None,
) -> UsageReport:
    dims = [d for d in group_by if d in DIMENSIONS]
    with_session = "session" in dims
    sql = f"""
        SELECT u.agent_id, a.host_id, h.hostname, a.name AS agent_name, a.agent_type,
               COALESCE(u.response_model, u.request_model) AS model, u.provider,
               substr(u.ts, 1, 13) AS hour, u.quality,
               {"u.session_id" if with_session else "NULL"} AS grouped_session,
               (u.input_tokens IS NOT NULL AND u.output_tokens IS NOT NULL) AS known,
               (COALESCE(u.input_tokens, 0) + COALESCE(u.cache_read_input_tokens, 0)
                + COALESCE(u.cache_creation_input_tokens, 0)) > {TIER_PROMPT_TOKENS} AS big,
               COUNT(*) AS n,
               SUM(u.input_tokens) AS input, SUM(u.output_tokens) AS output,
               SUM(u.cache_read_input_tokens) AS cache_read,
               SUM(u.cache_creation_input_tokens) AS cache_write,
               SUM(u.cache_creation_1h_input_tokens) AS cache_write_1h
        FROM usage_records u
        JOIN agents a ON a.agent_id = u.agent_id
        JOIN hosts h ON h.host_id = a.host_id
        WHERE u.ts >= ? AND u.ts < ? {"AND u.agent_id = ?" if agent_id else ""}
        GROUP BY u.agent_id, model, u.provider, hour, u.quality, grouped_session, known, big
    """  # noqa: S608 - only constant fragments are interpolated
    params: list[Any] = [ts(start), ts(end)]
    if agent_id:
        params.append(agent_id)
    with db.read() as conn:
        groups = conn.execute(sql, params).fetchall()

    rows: dict[tuple[str, ...], UsageRow] = {}
    total = UsageRow()
    for g in groups:
        key = {
            "agent": g["agent_id"],
            "host": g["host_id"],
            "model": g["model"] or "unknown",
            "day": _local_day(g["hour"], tz).isoformat(),
            "session": g["grouped_session"] or "",
        }
        labels = {
            "agent": f"{g['agent_name']} · {g['hostname']}",
            "host": g["hostname"],
            "model": g["model"] or "unknown",
            "day": key["day"],
            "session": key["session"],
        }
        group_key = tuple(key[d] for d in dims)
        row = rows.setdefault(
            group_key,
            UsageRow(key={d: key[d] for d in dims}, labels={d: labels[d] for d in dims}),
        )
        for target in (row, total):
            _add(target, g, prices)
    reading = _reading(db, end, agent_id)
    for row in [*rows.values(), total]:
        row.history_complete = not any(_misses(r, row, tz) for r in reading)
    ordered = sorted(rows.values(), key=lambda r: tuple(r.key.values()))
    return UsageReport(ordered, total, start, end, reading)


def _reading(db: Database, end: datetime, agent_id: str | None) -> list[Reading]:
    """Agents still reading their history, when that history reaches into the period."""
    sql = f"""
        SELECT b.agent_id, b.read_until, b.bytes_left, a.host_id, a.name, h.hostname
        FROM usage_backfill b
        JOIN agents a ON a.agent_id = b.agent_id
        JOIN hosts h ON h.host_id = a.host_id
        WHERE b.complete = 0 AND (b.read_until IS NULL OR b.read_until < ?)
              {"AND b.agent_id = ?" if agent_id else ""}
        ORDER BY a.name, h.hostname
    """  # noqa: S608 - only constant fragments are interpolated
    params = [ts(end), agent_id] if agent_id else [ts(end)]
    with db.read() as conn:
        found = conn.execute(sql, params).fetchall()
    return [Reading(r["agent_id"], r["host_id"], f"{r['name']} · {r['hostname']}",
                    parse_ts(r["read_until"]), r["bytes_left"]) for r in found]  # fmt: skip


def _misses(reading: Reading, row: UsageRow, tz: tzinfo) -> bool:
    """Whether usage not read yet could belong to this row."""
    if row.key.get("agent", reading.agent_id) != reading.agent_id:
        return False
    if row.key.get("host", reading.host_id) != reading.host_id:
        return False
    if "day" in row.key and reading.read_until is not None:
        day_end = datetime.combine(date.fromisoformat(row.key["day"]) + timedelta(days=1),
                                   time.min, tzinfo=tz)  # fmt: skip
        return reading.read_until < day_end
    return True


def _add(row: UsageRow, g: Any, prices: PriceTable) -> None:
    n = int(g["n"])
    quality = UsageQuality(g["quality"])
    row.records += n
    if not g["known"]:
        row.unknown += n
        return
    row.input_tokens += g["input"] or 0
    row.output_tokens += g["output"] or 0
    row.cache_read_tokens += g["cache_read"] or 0
    row.cache_write_tokens += g["cache_write"] or 0
    if quality is UsageQuality.EXACT:
        row.exact += n
    else:
        row.estimated += n
    synthetic = UsageRecord(
        record_id="group",
        agent_id=g["agent_id"],
        timestamp=datetime.now(UTC),
        provider=g["provider"],
        response_model=g["model"],
        input_tokens=g["input"] or 0,
        output_tokens=g["output"] or 0,
        cache_read_input_tokens=g["cache_read"],
        cache_creation_input_tokens=g["cache_write"],
        cache_creation_1h_input_tokens=g["cache_write_1h"],
        quality=quality,
    )
    cost = compute_cost(synthetic, prices, long_prompt=bool(g["big"]))
    if cost.amount is None:
        row.unpriced_tokens += (g["input"] or 0) + (g["output"] or 0)
    else:
        row.cost += cost.amount
        price = prices.find(g["provider"], g["model"])
        if price is not None and price.unverified:
            row.unverified_models.add(price.model_id)
