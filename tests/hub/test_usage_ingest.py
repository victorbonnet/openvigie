import dataclasses
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.usage import UsageStore
from openvigie.model import AgentInstance, Host, Session, UsageQuality, UsageRecord, to_dict

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
MAC = Host("h-mac", "mbp", "darwin", "arm64", "0.0.1")


def setup() -> tuple[TestClient, Database, str]:
    db = Database(MEMORY)
    inv = Inventory(db)
    for host in (PI, MAC):
        inv.upsert_host(host, NOW)
    inv.upsert_agents([AgentInstance("a1", "h-pi", "codex", "Codex"),
                       AgentInstance("m1", "h-mac", "codex", "Codex")], NOW)  # fmt: skip
    token = Credentials(db).issue_token("h-pi", NOW)
    return TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878"), db, token


def record(rid: str = "r1", agent: str = "a1", **kw: Any) -> UsageRecord:
    base = UsageRecord(rid, agent, NOW, session_id="s1", provider="openai",
                       response_model="gpt-5", input_tokens=100, output_tokens=10,
                       quality=UsageQuality.EXACT)  # fmt: skip
    return dataclasses.replace(base, **kw)


def push(client: TestClient, token: str, **sections: Any) -> Any:
    body = {"schema_version": 1, "host": to_dict(PI), "sent_at": NOW.isoformat(), **sections}
    return client.post("/api/v1/ingest", json=body, headers={"authorization": f"Bearer {token}"})


def test_usage_and_sessions_ingested_and_deduped() -> None:
    client, db, token = setup()
    usage = [to_dict(record("r1")), to_dict(record("r2", input_tokens=None, output_tokens=None,
                                                    quality=UsageQuality.UNKNOWN))]  # fmt: skip
    sessions = [to_dict(Session("s1", "a1", NOW, NOW, ("gpt-5",)))]
    first = push(client, token, usage=usage, sessions=sessions).json()["accepted"]
    assert first["usage"] == 2 and first["sessions"] == 1
    assert push(client, token, usage=usage).json()["accepted"]["usage"] == 0  # replay
    stored = {r.record_id: r for r in UsageStore(db).records()}
    assert stored["r2"].input_tokens is None  # unknown stays NULL, not 0
    assert stored["r1"].input_tokens == 100


def test_more_precise_replay_upgrades_record() -> None:
    client, db, token = setup()
    push(client, token, usage=[to_dict(record(input_tokens=None, quality=UsageQuality.UNKNOWN))])
    push(client, token, usage=[to_dict(record(input_tokens=100))])
    push(client, token, usage=[to_dict(record(input_tokens=None, quality=UsageQuality.UNKNOWN))])
    (stored,) = UsageStore(db).records()
    assert stored.input_tokens == 100 and stored.quality is UsageQuality.EXACT


def test_usage_for_other_hosts_agents_is_dropped() -> None:
    client, db, token = setup()
    response = push(client, token, usage=[to_dict(record("x", agent="m1"))])
    assert response.json()["accepted"]["usage"] == 0
    assert UsageStore(db).records() == []


def test_sessions_merge_bounds_and_models() -> None:
    _, db, _ = setup()
    store = UsageStore(db)
    store.add_sessions([Session("s1", "a1", NOW, NOW, ("gpt-5",))], "h-pi")
    later = NOW + timedelta(hours=1)
    store.add_sessions([Session("s1", "a1", later, later, ("gpt-5-codex",))], "h-pi")
    (session,) = store.sessions("a1")
    assert session.started_at == NOW and session.last_event_at == later
    assert session.models == ("gpt-5", "gpt-5-codex")


def test_spool_drops_reported() -> None:
    client, db, token = setup()
    push(client, token, collector={"spool_dropped": 3})
    host = Inventory(db).host("h-pi")
    assert host is not None and host.spool_dropped == 3
    html = client.get("/hosts/h-pi").text
    assert "3 payloads dropped" in html


def test_ten_thousand_records_ingest_quickly() -> None:
    _, db, _ = setup()
    records = [record(f"r{i}") for i in range(10_000)]
    start = time.perf_counter()
    assert UsageStore(db).add_records(records, "h-pi") == 10_000
    assert time.perf_counter() - start < 3
