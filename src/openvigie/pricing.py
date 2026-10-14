"""Tokens to cost (FR-4), with a bundled price table and a user override file.

Cost is computed at query time from the current table, so editing prices re-prices history.
A model missing from the table, or a record with unknown token counts, gives an unknown cost
(``amount is None``), never $0. Amounts are ``Decimal``; round only for display.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from openvigie.config import ConfigError, config_dir
from openvigie.model import UsageQuality, UsageRecord
from openvigie.safeio import load_toml_bounded

MILLION = Decimal(1_000_000)
DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


@dataclass(frozen=True)
class Rates:
    input: Decimal
    output: Decimal
    cache_read: Decimal | None = None
    cache_write_5m: Decimal | None = None
    cache_write_1h: Decimal | None = None


@dataclass(frozen=True)
class ModelPrice:
    provider: str
    model_id: str
    rates: Rates
    aliases: tuple[str, ...] = ()
    above_prompt_tokens: int | None = None
    above: Rates | None = None
    as_of: str | None = None
    unverified: bool = False
    overridden: bool = False


@dataclass
class PriceTable:
    models: dict[tuple[str, str], ModelPrice] = field(default_factory=dict)
    cached_input_included: dict[str, bool] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    override_path: Path | None = None

    def find(self, provider: str | None, model: str | None) -> ModelPrice | None:
        if not model:
            return None
        candidates = [model, DATE_SUFFIX.sub("", model)]
        for name in candidates:
            for price in self.models.values():
                if provider is not None and price.provider != provider:
                    continue
                if name == price.model_id or name in price.aliases:
                    return price
        return None


@dataclass(frozen=True)
class Cost:
    amount: Decimal | None
    quality: UsageQuality
    reason: str | None = None  # why the amount is unknown or estimated


def compute_cost(record: UsageRecord, table: PriceTable) -> Cost:
    model = record.response_model or record.request_model
    price = table.find(record.provider, model)
    if price is None:
        return Cost(None, UsageQuality.UNKNOWN, "unpriced_model")
    if record.input_tokens is None or record.output_tokens is None:
        return Cost(None, UsageQuality.UNKNOWN, "unknown_tokens")
    included = table.cached_input_included.get(price.provider, False)
    cache_read = record.cache_read_input_tokens or 0
    written = record.cache_creation_input_tokens or 0
    written_1h = min(record.cache_creation_1h_input_tokens or 0, written)
    written_5m = written - written_1h
    uncached = max(record.input_tokens - cache_read, 0) if included else record.input_tokens
    prompt = record.input_tokens if included else uncached + cache_read + written

    rates = price.rates
    if price.above and price.above_prompt_tokens is not None and prompt > price.above_prompt_tokens:
        rates = price.above
    quality = record.quality
    reason = None

    def rate(value: Decimal | None, tokens: int) -> Decimal:
        nonlocal quality, reason
        if value is not None or tokens == 0:
            return value or Decimal(0)
        quality, reason = UsageQuality.ESTIMATED, "missing_cache_price"
        return rates.input  # best guess: bill at the input rate

    total = (
        uncached * rates.input
        + record.output_tokens * rates.output
        + cache_read * rate(rates.cache_read, cache_read)
        + written_5m * rate(rates.cache_write_5m, written_5m)
        + written_1h * rate(rates.cache_write_1h, written_1h)
    ) / MILLION
    if quality is UsageQuality.UNKNOWN:
        quality = UsageQuality.ESTIMATED
    return Cost(total, quality, reason)


# --- loading ---------------------------------------------------------------------------------


def _decimal(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ConfigError(f"{where}: price must be a non-negative number")
    return Decimal(str(value))


def _rates(data: dict[str, Any], where: str) -> Rates:
    for required in ("input", "output"):
        if required not in data:
            raise ConfigError(f"{where}: missing {required}")
    values = {k: _decimal(data[k], f"{where}.{k}") for k in PRICE_FIELDS if k in data}
    return Rates(**values)


def _parse(data: dict[str, Any], table: PriceTable, *, overridden: bool, where: str) -> None:
    for name, provider in (data.get("providers") or {}).items():
        if not isinstance(provider, dict):
            raise ConfigError(f"{where}: providers.{name} must be a table")
        if "cached_input_included" in provider:
            table.cached_input_included[name] = bool(provider["cached_input_included"])
        if isinstance(provider.get("source"), str):
            table.sources[name] = provider["source"]
    for index, entry in enumerate(data.get("models") or []):
        here = f"{where}: models[{index}]"
        if not isinstance(entry, dict) or not entry.get("provider") or not entry.get("id"):
            raise ConfigError(f"{here}: needs provider and id")
        above = entry.get("above")
        threshold = above.get("prompt_tokens") if isinstance(above, dict) else None
        price = ModelPrice(
            provider=str(entry["provider"]),
            model_id=str(entry["id"]),
            rates=_rates(entry, here),
            aliases=tuple(str(a) for a in entry.get("aliases", [])),
            above_prompt_tokens=int(threshold) if threshold is not None else None,
            above=_rates(above, here + ".above") if isinstance(above, dict) else None,
            as_of=str(entry["as_of"]) if entry.get("as_of") else None,
            unverified=bool(entry.get("unverified", False)),
            overridden=overridden,
        )
        table.models[(price.provider, price.model_id)] = price


def load_price_table(override: Path | None = None) -> PriceTable:
    """Bundled prices, then the user's override file (default ``<config dir>/prices.toml``)."""
    table = PriceTable()
    bundled = (resources.files("openvigie") / "prices.toml").read_text(encoding="utf-8")
    _parse(tomllib.loads(bundled), table, overridden=False, where="bundled prices.toml")
    path = override or config_dir() / "prices.toml"
    table.override_path = path
    if path.exists():
        try:
            data = load_toml_bounded(path, max_bytes=256 * 1024)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{path}: invalid TOML ({exc})") from None
        _parse(data, table, overridden=True, where=str(path))
    return table
