import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openvigie.adapters.spans import trace_id
from openvigie.config import ConfigError, HubSettings, LangfuseSettings, load_hub_settings
from openvigie.hub.app import create_app
from openvigie.hub.langfuse import trace_link
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.spans import SpanQueue
from openvigie.hub.store.usage import UsageStore
from openvigie.model import AgentInstance, Host, Span, UsageQuality, UsageRecord

NOW = datetime.now(UTC).replace(microsecond=0)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
LF = "https://lf.example.com"


@pytest.fixture(autouse=True)
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")


def page(langfuse: LangfuseSettings | None) -> str:
    db = Database(MEMORY)
    inv = Inventory(db)
    inv.upsert_host(PI, NOW)
    inv.upsert_agents([AgentInstance("a1", "h-pi", "claude-code", "Claude Code")], NOW)
    UsageStore(db).add_records(
        [UsageRecord(f"r{i}", "a1", NOW - timedelta(minutes=i), session_id=sid,
                     provider="anthropic", response_model="claude-opus-5-5", input_tokens=10,
                     output_tokens=10 * (i + 1), quality=UsageQuality.EXACT)
         for i, sid in enumerate(["s-exported", "s-pending"])],
        "h-pi",
    )  # fmt: skip
    queue = SpanQueue(db)
    exported = trace_id("a1", "s-exported")
    queue.enqueue([Span(exported, "r" * 16, "invoke_agent claude-code", NOW, NOW,
                        agent_id="a1")], "h-pi", NOW)  # fmt: skip
    queue.mark_exported(queue.due(NOW, 10), NOW)
    app = create_app(HubSettings(langfuse=langfuse), db)
    return TestClient(app, base_url="http://127.0.0.1:7878").get("/agents/a1").text


def links(html: str) -> list[str]:
    return re.findall(r'<a href="([^"]+)" target="_blank" rel="noopener noreferrer">', html)


def test_configured_and_exported_links_to_the_trace() -> None:
    html = page(LangfuseSettings(LF, "pk-lf-1", project_id="proj_1"))
    trace = trace_id("a1", "s-exported")
    assert links(html) == [f"{LF}/project/proj_1/traces", f"{LF}/project/proj_1/traces/{trace}"]
    assert "View trace ↗" in html
    assert "not exported" in html  # the other session has no trace in Langfuse


def test_unconfigured_shows_no_links_and_no_column() -> None:
    html = page(None)
    assert links(html) == [] and "<th>Trace</th>" not in html


def test_exported_without_project_id_explains() -> None:
    html = page(LangfuseSettings(LF, "pk-lf-1"))
    assert links(html) == []
    assert "set langfuse.project_id for links" in html


def test_trace_link_and_project_id_validation(tmp_path: Path) -> None:
    lf = LangfuseSettings(LF + "/", "pk-lf-1", project_id="p")
    assert trace_link(lf, "ab" * 16, True).url == f"{LF}/project/p/traces/{'ab' * 16}"
    assert trace_link(lf, "ab" * 16, False).reason == "not exported"
    path = tmp_path / "hub.toml"
    path.write_text(f'[langfuse]\nhost = "{LF}"\npublic_key = "pk-lf-1"\nproject_id = "a/../b"\n')
    with pytest.raises(ConfigError, match="project_id"):
        load_hub_settings(path)
