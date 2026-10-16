"""Usage and cost aggregation (FR-3, FR-4), shared by the UI and ``/api/v1/usage``.

SQLite sums tokens per (agent, model, provider, hour, quality, prompt-size bucket); Python then
prices each group (cost is linear in tokens within a tier) and folds groups into the requested
dimensions, bucketing days in the display timezone. Every row carries its coverage: how many
records were exact, estimated or unknown, and how many tokens had no price. Unknown is never
summed as zero; a row with any unknown or unpriced part reports its cost as a lower bound.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from decimal import Decimal
from typing import Any

from openvigie.hub.store.db import Database, ts
from openvigie.model import UsageQuality, UsageRecord
from openvigie.pricing import PriceTable, compute_cost

DIMENSIONS = ("agent", "host", "model", "day", "session")


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

    @property
    def tokens(self) -> int:
        return (self.input_tokens + self.output_tokens + self.cache_read_tokens
                + self.cache_write_tokens)  # fmt: skip

    @property
    def cost_complete(self) -> bool:
        """False when part of the usage could not be priced: ``cost`` is then a lower bound."""
        return self.unknown == 0 and self.unpriced_tokens == 0

    @property
    def cost_usd(self) -> str:
        """The cost as a decimal string, to the micro-dollar."""
        return str(self.cost.quantize(Decimal("0.000001")))


@dataclass
class UsageReport:
    rows: list[UsageRow]
    total: UsageRow
    start: datetime
    end: datetime


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
    # Tiers depend on each request's own prompt size, so requests are bucketed by how many of
    # the table's tier thresholds they exceed before summing.
    thresholds = _thresholds(prices)
    bucket = " + ".join(f"(u.prompt > {t})" for t in thresholds) or "0"
    included = [name for name, inside in prices.cached_input_included.items() if inside]
    marks = ", ".join("?" for _ in included) or "NULL"
    sql = f"""
        WITH r AS (
            SELECT *, CASE WHEN provider IN ({marks}) THEN COALESCE(input_tokens, 0)
                      ELSE COALESCE(input_tokens, 0) + COALESCE(cache_read_input_tokens, 0)
                           + COALESCE(cache_creation_input_tokens, 0) END AS prompt
            FROM usage_records WHERE ts >= ? AND ts < ? {"AND agent_id = ?" if agent_id else ""}
        )
        SELECT u.agent_id, a.host_id, h.hostname, a.name AS agent_name, a.agent_type,
               COALESCE(u.response_model, u.request_model) AS model, u.provider,
               substr(u.ts, 1, 13) AS hour, u.quality,
               {"u.session_id" if with_session else "NULL"} AS grouped_session,
               (u.input_tokens IS NOT NULL AND u.output_tokens IS NOT NULL) AS known,
               {bucket} AS bucket,
               COUNT(*) AS n,
               SUM(u.input_tokens) AS input, SUM(u.output_tokens) AS output,
               SUM(u.cache_read_input_tokens) AS cache_read,
               SUM(u.cache_creation_input_tokens) AS cache_write,
               SUM(u.cache_creation_1h_input_tokens) AS cache_write_1h
        FROM r u
        JOIN agents a ON a.agent_id = u.agent_id
        JOIN hosts h ON h.host_id = a.host_id
        GROUP BY u.agent_id, model, u.provider, hour, u.quality, grouped_session, known, bucket
    """  # noqa: S608 - only constant fragments and validated integers are interpolated
    params: list[Any] = [*included, ts(start), ts(end)]
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
            _add(target, g, prices, thresholds)
    ordered = sorted(rows.values(), key=lambda r: tuple(r.key.values()))
    return UsageReport(ordered, total, start, end)


def _thresholds(prices: PriceTable) -> list[int]:
    return sorted({p.above_prompt_tokens for p in prices.models.values()
                   if p.above_prompt_tokens is not None})  # fmt: skip


def _add(row: UsageRow, g: Any, prices: PriceTable, thresholds: list[int]) -> None:
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
    price = prices.find(g["provider"], g["model"])
    limit = price.above_prompt_tokens if price else None
    long_prompt = limit is not None and g["bucket"] > thresholds.index(limit)
    cost = compute_cost(synthetic, prices, long_prompt=long_prompt)
    if cost.amount is None:
        row.unpriced_tokens += (g["input"] or 0) + (g["output"] or 0)
    else:
        row.cost += cost.amount
        if price is not None and price.unverified:
            row.unverified_models.add(price.model_id)
