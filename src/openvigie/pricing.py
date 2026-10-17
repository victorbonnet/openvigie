"""Tokens to cost (FR-4), with a bundled price table and a user override file.

Cost is computed at query time from the current table, so editing prices re-prices history.
A model missing from the table, or a record with unknown token counts, gives an unknown cost
(``amount is None``), never $0. Amounts are ``Decimal``; round only for display.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BeforeValidator,
    Field,
    Strict,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from openvigie.config import ConfigError, config_dir, describe, file_entry
from openvigie.model import UsageQuality, UsageRecord
from openvigie.safeio import load_toml_bounded

MILLION = Decimal(1_000_000)
DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def _price(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("a price must be a number")
    return Decimal(str(value))


Price = Annotated[Decimal, BeforeValidator(_price), Field(ge=0)]  # USD per million tokens


@file_entry
class Rates:
    input: Price
    output: Price
    cache_read: Price | None = None
    cache_write_5m: Price | None = None
    cache_write_1h: Price | None = None


@file_entry
class Tier(Rates):
    """``above = { prompt_tokens = N, ... }``: rates for prompts longer than N tokens."""

    prompt_tokens: Annotated[int, Strict(), Field(gt=0)]


@file_entry
class ModelEntry(Rates):
    """One ``[[models]]`` entry of a price file."""

    provider: Annotated[str, Strict(), Field(min_length=1)]
    id: Annotated[str, Strict(), Field(min_length=1)]
    aliases: tuple[StrictStr, ...] = ()
    above: Tier | None = None
    as_of: StrictStr | date | None = None
    unverified: StrictBool = False
    source: StrictStr | None = None  # where a generated entry comes from, e.g. litellm@<commit>


@file_entry
class ProviderEntry:
    """``[providers.<name>]``: true when the provider counts cached tokens inside input_tokens."""

    cached_input_included: StrictBool = False
    source: StrictStr | None = None


@file_entry
class PriceFile:
    providers: dict[StrictStr, ProviderEntry] = field(default_factory=dict)
    models: tuple[ModelEntry, ...] = ()


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
    source: str | None = None


@dataclass
class PriceTable:
    models: dict[tuple[str, str], ModelPrice] = field(default_factory=dict)
    cached_input_included: dict[str, bool] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    override_path: Path | None = None
    # name or alias -> prices in table order, so the first match still wins; reset by _add
    _index: dict[str, list[ModelPrice]] | None = field(default=None, repr=False)

    def find(self, provider: str | None, model: str | None) -> ModelPrice | None:
        if not model:
            return None
        if self._index is None:
            self._index = {}
            for price in self.models.values():
                for name in (price.model_id, *price.aliases):
                    self._index.setdefault(name, []).append(price)
        for name in (model, DATE_SUFFIX.sub("", model)):
            for price in self._index.get(name, ()):
                if provider is None or price.provider == provider:
                    return price
        return None


@dataclass(frozen=True)
class Cost:
    amount: Decimal | None
    quality: UsageQuality
    reason: str | None = None  # why the amount is unknown or estimated


def compute_cost(
    record: UsageRecord, table: PriceTable, *, long_prompt: bool | None = None
) -> Cost:
    """Price one record. ``long_prompt`` overrides the tier choice when ``record`` sums several
    requests (aggregation), since tiers depend on each request's own prompt size."""
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
    # OpenAI's input_tokens include cache reads and writes (input_tokens_details): bill each once
    inside = cache_read + written if included else 0
    uncached = max(record.input_tokens - inside, 0)
    inconsistent = inside > record.input_tokens
    prompt = record.input_tokens if included else uncached + cache_read + written

    rates = price.rates
    if long_prompt is None:
        long_prompt = price.above_prompt_tokens is not None and prompt > price.above_prompt_tokens
    if price.above and long_prompt:
        rates = price.above
    quality = record.quality
    reason = None
    if inconsistent:  # more cached than input: the counts are wrong, so the cost is a guess
        quality, reason = UsageQuality.ESTIMATED, "inconsistent_tokens"

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


def _add(file: PriceFile, table: PriceTable, *, overridden: bool) -> None:
    table._index = None
    for name, provider in file.providers.items():
        table.cached_input_included[name] = provider.cached_input_included
        if provider.source:
            table.sources[name] = provider.source
    for entry in file.models:
        rates = Rates(
            input=entry.input,
            output=entry.output,
            cache_read=entry.cache_read,
            cache_write_5m=entry.cache_write_5m,
            cache_write_1h=entry.cache_write_1h,
        )
        table.models[(entry.provider, entry.id)] = ModelPrice(
            provider=entry.provider,
            model_id=entry.id,
            rates=rates,
            aliases=entry.aliases,
            above_prompt_tokens=entry.above.prompt_tokens if entry.above else None,
            above=entry.above,
            as_of=str(entry.as_of) if entry.as_of else None,
            unverified=entry.unverified,
            overridden=overridden,
            source=entry.source,
        )  # fmt: skip


def _parse(data: dict[str, Any], where: str) -> PriceFile:
    try:
        return TypeAdapter(PriceFile).validate_python(data)
    except ValidationError as exc:
        raise ConfigError(f"{where}: {describe(exc, 'prices')}") from None


def load_price_table(override: Path | None = None) -> PriceTable:
    """Bundled prices, then the user's override file (default ``<config dir>/prices.toml``)."""
    table = PriceTable()
    bundled = (resources.files("openvigie") / "prices.toml").read_text(encoding="utf-8")
    _add(_parse(tomllib.loads(bundled), "bundled prices.toml"), table, overridden=False)
    path = override or config_dir() / "prices.toml"
    table.override_path = path
    if path.exists():
        try:
            data = load_toml_bounded(path, max_bytes=256 * 1024)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{path}: invalid TOML ({exc})") from None
        _add(_parse(data, str(path)), table, overridden=True)
    return table
