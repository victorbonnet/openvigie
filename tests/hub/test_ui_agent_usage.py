from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.usage import UsageStore
from openvigie.model import AgentInstance, Host, Session, UsageQuality, UsageRecord

NOW = datetime.now(UTC).replace(microsecond=0)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")


def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: str = "") -> TestClient:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    if override:
        (tmp_path / "prices.toml").write_text(override)
    db = Database(MEMORY)
    inv = Inventory(db)
    inv.upsert_host(PI, NOW)
    inv.upsert_agents([AgentInstance("a1", "h-pi", "codex", "Codex"),
                       AgentInstance("a2", "h-pi", "codex", "Idle")], NOW)  # fmt: skip
    store = UsageStore(db)
    store.add_records(
        [UsageRecord("r1", "a1", NOW - timedelta(minutes=30), session_id="s-123", provider="openai",
                     response_model="gpt-5", input_tokens=1_000_000, output_tokens=0,
                     quality=UsageQuality.EXACT),
         UsageRecord("r2", "a1", NOW, session_id="s-123", provider="openai",
                     response_model="secret-model", input_tokens=10, output_tokens=10,
                     quality=UsageQuality.EXACT)],
        "h-pi",
    )  # fmt: skip
    store.add_sessions([Session("s-123", "a1", NOW - timedelta(minutes=30), NOW, ("gpt-5",))],
                       "h-pi")  # fmt: skip
    app = create_app(HubSettings(display_timezone="UTC"), db)
    return TestClient(app, base_url="http://127.0.0.1:7878")


def test_agent_page_usage_section(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = setup(monkeypatch, tmp_path).get("/agents/a1").text
    assert "Usage · last 30 days" in html
    assert "≥ $1.25" in html  # one call is unpriced, so the total is a lower bound
    assert '<svg class="chart"' in html
    assert "<code>s-123</code>" in html and "30 min" in html and "gpt-5" in html


def test_agent_without_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = setup(monkeypatch, tmp_path).get("/agents/a2").text
    assert "No usage recorded in the last 30 days." in html


def test_prices_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = '[[models]]\nprovider = "openai"\nid = "gpt-5"\ninput = 2\noutput = 20\n'
    html = setup(monkeypatch, tmp_path, override).get("/prices").text
    assert "Seen in the last 90 days but unpriced" in html and "secret-model" in html
    assert "your override" in html
    assert "bundled · unverified" in html
    assert "claude-opus-5-5" in html
    assert str(tmp_path / "prices.toml") in html
