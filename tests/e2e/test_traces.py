"""Collector → hub → Langfuse: spans flow only once the hub exports traces (FR-6)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_single_host import FakeMachine, Hub

from openvigie import semconv
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import TEST_HOST
from openvigie.collector.client import HubClient
from openvigie.collector.daemon import Collector
from openvigie.collector.enrol import enrol
from openvigie.config import CollectorSettings, HubSettings, LangfuseSettings, read_private_file
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.spans import SpanQueue

pytestmark = pytest.mark.e2e


def collector(hub: Hub, tmp_path: Path, **settings: Any) -> Collector:
    machine = FakeMachine(tmp_path)
    config = CollectorSettings(homes=(machine.home,), token_file=tmp_path / "state" / "token",
                               **settings)  # fmt: skip
    code = Credentials(hub.db).create_enrol_code(datetime.now(UTC), timedelta(minutes=5))
    token = enrol(hub.url, code, TEST_HOST, config, config_path=tmp_path / "c.toml")
    client = HubClient(hub.url, token=read_private_file(token))
    discovery = Discovery(adapters=[ClaudeCodeAdapter(), CodexAdapter()])
    return Collector(config, machine.context(), discovery, client)


def test_no_spans_without_langfuse(tmp_path: Path) -> None:
    with Hub(tmp_path) as hub:
        c = collector(hub, tmp_path)
        assert c.tick()
        assert not c.ctx.traces.enabled
        assert SpanQueue(hub.db).stats().pending == 0


def test_spans_reach_langfuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-e2e")
    lf = LangfuseSettings(host="https://cloud.langfuse.com", public_key="pk-lf-e2e",
                          export_content=True)  # fmt: skip
    sent: list[dict[str, Any]] = []

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        sent.append(json.loads(body))
        return 200

    with Hub(tmp_path, HubSettings(langfuse=lf)) as hub:
        hub.app.state.exporter.transport = transport
        c = collector(hub, tmp_path)  # this collector does not consent to content
        assert c.tick()
        assert c.ctx.traces.enabled and not c.ctx.traces.export_content
        queued = SpanQueue(hub.db).stats().pending
        assert queued > 40  # Claude Code (37) + Codex sessions
        assert hub.app.state.exporter.run_once() == queued

    spans = [s for p in sent for r in p["resourceSpans"] for sc in r["scopeSpans"]
             for s in sc["spans"]]  # fmt: skip
    roots = {s["name"] for s in spans if "parentSpanId" not in s}
    assert roots == {"invoke_agent claude-code", "invoke_agent codex"}
    keys = {a["key"] for s in spans for a in s["attributes"]}
    assert "langfuse.session.id" in keys
    assert not keys & semconv.CONTENT_ATTRIBUTES  # hub allows content, collector did not
