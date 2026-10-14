from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from openvigie.config import ConfigError
from openvigie.model import UsageQuality, UsageRecord
from openvigie.pricing import compute_cost, load_price_table

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def rec(provider: str = "anthropic", model: str = "claude-opus-5-5", **kw: Any) -> UsageRecord:
    defaults: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0,
                                "quality": UsageQuality.EXACT}  # fmt: skip
    defaults.update(kw)
    return UsageRecord("r", "a", NOW, provider=provider, response_model=model, **defaults)


@pytest.fixture
def table(tmp_path: Path) -> Any:
    return load_price_table(tmp_path / "no-override.toml")


def test_anthropic_cache_tokens_are_separate(table: Any) -> None:
    cost = compute_cost(
        rec(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_input_tokens=1_000_000,
            cache_creation_input_tokens=2_000_000, cache_creation_1h_input_tokens=1_000_000),
        table,
    )  # fmt: skip
    # 4 input + 20 output + 0.20 read + 5 (5m write) + 8 (1h write)
    assert cost.amount == Decimal("37.2")
    assert cost.quality is UsageQuality.EXACT and cost.reason is None


def test_openai_cached_input_is_subset_of_input(table: Any) -> None:
    cost = compute_cost(
        rec("openai", "gpt-5-codex", input_tokens=12000, cache_read_input_tokens=4800,
            output_tokens=650, reasoning_tokens=128),
        table,
    )  # fmt: skip
    expected = (Decimal(7200) * Decimal("1.25") + Decimal(4800) * Decimal("0.125")
                + Decimal(650) * Decimal(10)) / Decimal(1_000_000)  # fmt: skip
    assert cost.amount == expected


def test_recorded_claude_fixture_cost(table: Any) -> None:
    # totals of the recorded Claude Code fixture (#44, #96)
    cost = compute_cost(
        rec(input_tokens=18, output_tokens=24009, cache_read_input_tokens=529331,
            cache_creation_input_tokens=190288, cache_creation_1h_input_tokens=54784),
        table,
    )  # fmt: skip
    assert cost.amount is not None
    assert cost.amount == Decimal("1.7019102")  # hand-computed, see PR


def test_dated_model_ids_and_aliases(table: Any) -> None:
    assert table.find("anthropic", "claude-sonnet-4-5-20250929").model_id == "claude-sonnet-4-6"
    assert table.find("anthropic", "claude-opus-4-8").model_id == "claude-opus-5"
    assert table.find(None, "gpt-5.1-codex").provider == "openai"
    assert table.find("openrouter", "gpt-5") is None


def test_unknowns_are_never_zero(table: Any) -> None:
    unpriced = compute_cost(rec(model="mystery-model", input_tokens=10), table)
    assert unpriced.amount is None and unpriced.reason == "unpriced_model"
    unknown = compute_cost(rec(input_tokens=None, quality=UsageQuality.UNKNOWN), table)
    assert unknown.amount is None and unknown.reason == "unknown_tokens"


def test_estimated_tokens_give_estimated_cost(table: Any) -> None:
    cost = compute_cost(rec(input_tokens=100, quality=UsageQuality.ESTIMATED), table)
    assert cost.quality is UsageQuality.ESTIMATED and cost.amount is not None


def test_haiku_long_prompt_tier(table: Any) -> None:
    short = compute_cost(rec(model="claude-haiku-5-5", input_tokens=100_000), table)
    long = compute_cost(rec(model="claude-haiku-5-5", input_tokens=100_001), table)
    assert short.amount == Decimal("0.01")
    assert long.amount == Decimal("100001") * Decimal("0.5") / Decimal(1_000_000)


def test_override_file_wins_and_adds(tmp_path: Path) -> None:
    override = tmp_path / "prices.toml"
    override.write_text(
        "[providers.openrouter]\ncached_input_included = true\n"
        '[[models]]\nprovider = "anthropic"\nid = "claude-opus-5-5"\ninput = 1\noutput = 2\n'
        '[[models]]\nprovider = "openrouter"\nid = "gpt-5"\ninput = 1.5\noutput = 11\n'
    )
    table = load_price_table(override)
    opus = table.find("anthropic", "claude-opus-5-5")
    assert opus is not None and opus.rates.input == Decimal(1) and opus.overridden
    router = table.find("openrouter", "gpt-5")
    assert router is not None and router.rates.output == Decimal(11)
    # override without cache prices: cache tokens billed at input rate, flagged estimated
    cost = compute_cost(rec(input_tokens=0, cache_read_input_tokens=1_000_000), table)
    assert cost.amount == Decimal(1) and cost.quality is UsageQuality.ESTIMATED
    assert cost.reason == "missing_cache_price"


@pytest.mark.parametrize(
    "text",
    [
        '[[models]]\nprovider = "x"\nid = "y"\ninput = -1\noutput = 1\n',
        '[[models]]\nprovider = "x"\nid = "y"\ninput = 1\n',
        '[[models]]\nid = "y"\ninput = 1\noutput = 1\n',
        '[[models]]\nprovider = "x"\nid = "y"\ninput = "1"\noutput = 1\n',
        '[[models]]\nprovider = "x"\nid = "y"\ninput = 1\noutput = 1\ncahce_read = 1\n',
        "not = = toml",
    ],
)
def test_invalid_override_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "prices.toml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=str(path)):
        load_price_table(path)
