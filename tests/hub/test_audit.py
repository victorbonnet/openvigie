"""The audit log (FR-17, D14): append-only, hash-chained, redacted, readable by everyone."""

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.audit_cli import run
from openvigie.hub.store.audit import GENESIS, MAX_VALUE_CHARS, AuditLog
from openvigie.hub.store.db import MEMORY, Database

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
SECRET = "sk-ant-api03-" + "x" * 40


def fill(log: AuditLog, n: int) -> None:
    for i in range(n):
        log.record(at=T0 + timedelta(minutes=i), actor="alice@example.com", role="admin",
                   action="policy.apply", target=f"ov-{i}", result="succeeded",
                   request_id=f"r{i}", before={"rev": i}, after={"rev": i + 1})  # fmt: skip


@pytest.fixture
def log() -> AuditLog:
    return AuditLog(Database(MEMORY))


def test_entries_chain_and_verify(log: AuditLog) -> None:
    fill(log, 3)
    first, second = sorted(log.all(), key=lambda e: e.seq)[:2]
    assert first.prev_hash == GENESIS and second.prev_hash == first.hash
    assert [e.seq for e in log.entries()] == [3, 2, 1]
    assert [e.seq for e in log.entries(limit=1, before_seq=3)] == [2]
    assert log.verify().ok and log.verify().checked == 3


def test_empty_log_verifies(log: AuditLog) -> None:
    assert log.verify().ok and log.verify().checked == 0


def test_an_edited_entry_breaks_the_chain(log: AuditLog) -> None:
    fill(log, 3)
    with log.db.transaction() as conn:
        conn.execute("UPDATE audit_log SET actor = 'mallory' WHERE seq = 2")
    result = log.verify()
    assert not result.ok and result.broken_at == 2 and result.reason == "entry was changed"


def test_an_edited_hash_breaks_the_next_link(log: AuditLog) -> None:
    fill(log, 3)
    with log.db.transaction() as conn:  # rewrite entry 2 consistently: entry 3 no longer links
        conn.execute("UPDATE audit_log SET hash = ? WHERE seq = 2", ("f" * 64,))
    result = log.verify()
    assert not result.ok and result.broken_at == 2


def test_a_deleted_entry_is_found(log: AuditLog) -> None:
    fill(log, 3)
    with log.db.transaction() as conn:
        conn.execute("DELETE FROM audit_log WHERE seq = 2")
    result = log.verify()
    assert not result.ok and result.broken_at == 3 and result.reason == "entry 2 is missing"


def test_swapped_entries_are_found(log: AuditLog) -> None:
    fill(log, 3)
    with log.db.transaction() as conn:
        conn.execute("UPDATE audit_log SET seq = 99 WHERE seq = 1")
        conn.execute("UPDATE audit_log SET seq = 1 WHERE seq = 2")
        conn.execute("UPDATE audit_log SET seq = 2 WHERE seq = 99")
    result = log.verify()
    assert not result.ok and result.broken_at == 1


def test_values_are_redacted_before_storing(log: AuditLog) -> None:
    entry = log.record(at=T0, actor="alice", role="admin", action="config.set", target="x",
                       result="succeeded", request_id="r", before={"api_key": SECRET},
                       after={"note": f"key {SECRET}"})  # fmt: skip
    assert SECRET not in entry.before + entry.after
    with log.db.read() as conn:
        assert SECRET not in json.dumps([tuple(r) for r in conn.execute("SELECT * FROM audit_log")])
    assert log.verify().ok


def test_large_values_and_long_strings_are_bounded(log: AuditLog) -> None:
    entry = log.record(at=T0, actor="a" * 1000, role="admin", action="x", target="t",
                       result="succeeded", request_id="r",
                       after={"blob": "y" * (MAX_VALUE_CHARS + 1)})  # fmt: skip
    assert len(entry.actor) == 256 and json.loads(entry.after)["truncated"] is True
    assert log.verify().ok


def settings_for(tmp_path: Path) -> HubSettings:
    return HubSettings(db_path=tmp_path / "hub.db")


def test_cli_verify_and_export(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    db = Database(settings.resolved_db_path())
    fill(AuditLog(db), 2)
    head = AuditLog(db).entries(limit=1)[0].hash
    db.close()
    out = io.StringIO()
    assert run(settings, "verify", out) == 0
    assert out.getvalue() == f"ok: 2 entries, chain intact, head {head}\n"
    out = io.StringIO()
    assert run(settings, "export", out) == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [e["seq"] for e in lines] == [1, 2] and lines[1]["hash"] == head


def test_cli_verify_reports_a_break(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    db = Database(settings.resolved_db_path())
    fill(AuditLog(db), 2)
    with db.transaction() as conn:
        conn.execute("UPDATE audit_log SET result = 'failed' WHERE seq = 1")
    db.close()
    out = io.StringIO()
    assert run(settings, "verify", out) == 1
    assert out.getvalue().startswith("BROKEN at entry 1: entry was changed")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    db = Database(MEMORY)
    fill(AuditLog(db), 3)
    AuditLog(db).record(at=T0, actor="<b>eve</b>", role="admin", action="x", target="t",
                        result="refused", request_id="r")  # fmt: skip
    return TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")


def test_api_lists_newest_first_with_paging(client: TestClient) -> None:
    body = client.get("/api/v1/audit", params={"limit": 2}).json()
    assert [e["seq"] for e in body["entries"]] == [4, 3] and body["next_before"] == 3
    body = client.get("/api/v1/audit", params={"limit": 2, "before": 3}).json()
    assert [e["seq"] for e in body["entries"]] == [2, 1] and body["next_before"] is None
    assert body["entries"][1]["action"] == "policy.apply"
    assert client.get("/api/v1/audit", params={"limit": 0}).status_code == 422


def test_ui_page_lists_entries_escaped(client: TestClient) -> None:
    html = client.get("/audit").text
    assert '<a href="/audit">Audit</a>' in html
    assert "<code>policy.apply</code>" in html and "ov-2" in html
    assert "&lt;b&gt;eve&lt;/b&gt;" in html and "<b>eve</b>" not in html
    assert "openvigie hub audit verify" in html


def test_ui_page_when_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    app = create_app(HubSettings(), Database(MEMORY))
    html = TestClient(app, base_url="http://127.0.0.1:7878").get("/audit").text
    assert "No admin actions yet" in html
