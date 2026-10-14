"""Cost and token budgets per day or month (FR-14, PRD D17), from ``<config dir>/budgets.toml``.

Offers ``Budget`` / ``BudgetFile`` (the file format), ``load_budgets`` (never raises: a broken
file is reported in ``problems`` and ignored), ``evaluate`` (each budget's use in its current
period), ``check`` (``evaluate``, then raise an alert the first time a budget reaches its
warning level or its limit in a period) and ``budgets_lifespan`` (``check`` every minute in the
hub). Periods start at local midnight, or on the first of
the month, in the hub's display time zone. Usage that couldn't be priced makes the cost a lower
bound (unknown ≠ zero): it can trigger an alert, never hide one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

from fastapi import FastAPI
from pydantic import (
    BeforeValidator,
    Field,
    Strict,
    StrictFloat,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from openvigie.config import config_dir, describe, file_entry
from openvigie.hub.reports import usage_report
from openvigie.hub.store.alerts import AlertStore
from openvigie.hub.store.db import Database
from openvigie.hub.store.inventory import Inventory
from openvigie.pricing import PriceTable
from openvigie.safeio import UnsafeFileError, load_toml_bounded

log = logging.getLogger(__name__)

MAX_BUDGETS = 100
Level = Literal["ok", "warning", "exceeded"]


def _decimal(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ValueError("expected a number")
    return Decimal(str(value))


Money = Annotated[Decimal, BeforeValidator(_decimal), Field(gt=0, max_digits=12)]
Name = Annotated[str, Strict(), Field(min_length=1, max_length=256)]


@file_entry
class Budget:
    """One ``[[budgets]]`` entry. Without a selector, it covers every agent."""

    id: Annotated[str, Strict(), Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    period: Literal["day", "month"] = "day"
    agent_type: Name | None = None
    host: Name | None = None  # a hostname
    agent_id: Name | None = None
    max_cost_usd: Money | None = None
    max_tokens: Annotated[int, Strict(), Field(gt=0, le=10**15)] | None = None
    warn_at: Annotated[StrictFloat, Field(gt=0, lt=1)] = 0.8

    @model_validator(mode="after")
    def _limited(self) -> Self:
        if self.max_cost_usd is None and self.max_tokens is None:
            raise ValueError(f"budget {self.id}: set max_cost_usd, max_tokens or both")
        return self

    @property
    def scope(self) -> str:
        parts = [f"{k} {v}" for k, v in (("type", self.agent_type), ("host", self.host),
                                         ("agent", self.agent_id)) if v]  # fmt: skip
        return ", ".join(parts) or "all agents"


@file_entry
class BudgetFile:
    budgets: Annotated[tuple[Budget, ...], Field(max_length=MAX_BUDGETS)] = ()


@dataclass
class BudgetSet:
    budgets: list[Budget] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    path: Path | None = None


def load_budgets(path: Path | None = None) -> BudgetSet:
    path = path or config_dir() / "budgets.toml"
    if not path.exists():
        return BudgetSet(path=path)
    try:
        parsed = TypeAdapter(BudgetFile).validate_python(load_toml_bounded(path, 256 * 1024))
    except ValidationError as exc:
        return _broken(path, describe(exc, "budgets"))
    except (ValueError, OSError, UnsafeFileError) as exc:
        return _broken(path, str(exc))
    ids = [b.id for b in parsed.budgets]
    if len(ids) != len(set(ids)):
        return _broken(path, "two budgets have the same id")
    return BudgetSet(list(parsed.budgets), path=path)


def _broken(path: Path, why: str) -> BudgetSet:
    log.warning("budgets: %s ignored: %s", path, why)
    return BudgetSet(problems=[f"{path} ignored: {why}"], path=path)


@dataclass(frozen=True)
class BudgetStatus:
    budget: Budget
    start: datetime  # of the current period, in the display time zone
    cost: Decimal
    cost_complete: bool  # False: some usage couldn't be priced, cost is a lower bound
    tokens: int
    level: Level

    @property
    def fraction(self) -> float:
        """The larger of cost/limit and tokens/limit."""
        b = self.budget
        parts = [float(self.cost / b.max_cost_usd) if b.max_cost_usd else 0.0,
                 self.tokens / b.max_tokens if b.max_tokens else 0.0]  # fmt: skip
        return max(parts)


def period_start(period: str, now: datetime, tz: tzinfo) -> datetime:
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.replace(day=1) if period == "month" else start


def evaluate(db: Database, prices: PriceTable, budgets: list[Budget], tz: tzinfo,
             now: datetime) -> list[BudgetStatus]:  # fmt: skip
    if not budgets:
        return []
    rows = {r.agent.agent_id: r for r in Inventory(db).agents()}
    reports = {}
    out = []
    for budget in budgets:
        start = period_start(budget.period, now, tz)
        if start not in reports:
            reports[start] = usage_report(db, prices, start=start, end=now + timedelta(seconds=1),
                                          group_by=("agent",), tz=tz).rows  # fmt: skip
        cost, tokens, complete = Decimal(0), 0, True
        for usage in reports[start]:
            row = rows.get(usage.key["agent"])
            if row is None or not _selects(budget, row.agent.agent_type, row.hostname,
                                           row.agent.agent_id):  # fmt: skip
                continue
            cost += usage.cost
            tokens += usage.tokens
            complete = complete and usage.cost_complete
        status = BudgetStatus(budget, start, cost, complete, tokens, "ok")
        out.append(BudgetStatus(budget, start, cost, complete, tokens, _level(status)))
    return out


def _selects(budget: Budget, agent_type: str, hostname: str, agent_id: str) -> bool:
    return ((budget.agent_type in (None, agent_type)) and (budget.host in (None, hostname))
            and (budget.agent_id in (None, agent_id)))  # fmt: skip


def _level(status: BudgetStatus) -> Level:
    if status.fraction >= 1:
        return "exceeded"
    return "warning" if status.fraction >= status.budget.warn_at else "ok"


def check(db: Database, prices: PriceTable, budgets: list[Budget], tz: tzinfo,
          now: datetime) -> list[BudgetStatus]:  # fmt: skip
    """Evaluate, and raise each alert once per budget, period and level."""
    statuses = evaluate(db, prices, budgets, tz, now)
    alerts = AlertStore(db)
    for s in statuses:
        if s.level == "ok":
            continue
        b = s.budget
        when = "today" if b.period == "day" else "this month"
        verb = "is over" if s.level == "exceeded" else f"reached {round(b.warn_at * 100)} % of"
        title = f"Budget {b.id} {verb} its limit {when}"
        detail = f"{b.scope}: {used(s)}"
        alerts.raise_once(f"budget:{b.id}:{s.start.date().isoformat()}:{s.level}", s.level,
                          title, detail, now)  # fmt: skip
    return statuses


def used(s: BudgetStatus) -> str:
    """'≥ $4.20 of $5.00; 1,200 of 2,000,000 tokens', for alerts and the UI."""
    b, parts = s.budget, []
    if b.max_cost_usd:
        bound = "" if s.cost_complete else "≥ "
        parts.append(f"{bound}${s.cost:.2f} of ${b.max_cost_usd:.2f}")
    if b.max_tokens:
        parts.append(f"{s.tokens:,} of {b.max_tokens:,} tokens")
    return "; ".join(parts)


INTERVAL_S = 60


@asynccontextmanager
async def budgets_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Check budgets every minute in a worker thread; reloads budgets.toml each time."""

    def run() -> None:
        budgets = load_budgets()
        check(app.state.db, app.state.prices, budgets.budgets, app.state.tz, datetime.now(UTC))

    async def loop() -> None:
        while True:
            try:
                await asyncio.to_thread(run)
            except Exception:
                log.exception("budget check failed")
            await asyncio.sleep(INTERVAL_S)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
