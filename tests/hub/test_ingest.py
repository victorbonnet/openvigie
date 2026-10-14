import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.model import (
    AdapterHealth,
    AgentInstance,
    AgentStatus,
    HealthState,
    Host,
    StatusEvent,
    to_dict,
)

PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
MAC = Host("h-mac", "mbp", "darwin", "arm64", "0.0.1")


@pytest.fixture
def db() -> Database:
    return Database(MEMORY)


@pytest.fixture
def client(db: Database) -> TestClient:
    return TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")


def token_for(db: Database, host: Host) -> str:
    Inventory(db).upsert_host(host, datetime.now(UTC))
    return Credentials(db).issue_token(host.host_id, datetime.now(UTC))


def agent(agent_id: str = "a1", host: Host = PI) -> AgentInstance:
    return AgentInstance(agent_id, host.host_id, "claude-code", "Claude Code",
                         status=AgentStatus.ACTIVE, process_running=True)  # fmt: skip


def payload(host: Host = PI, **sections: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "host": to_dict(host),
        "sent_at": datetime.now(UTC).isoformat(),
        "agents": [to_dict(agent(host=host))],
        "status_events": [],
        "adapter_health": [to_dict(AdapterHealth("claude-code", HealthState.OK))],
    }
    body.update(sections)
    return body


def push(client: TestClient, token: str | None, body: dict[str, Any]) -> Any:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/v1/ingest", json=body, headers=headers)


def test_happy_path_and_idempotent_replay(client: TestClient, db: Database) -> None:
    token = token_for(db, PI)
    events = [to_dict(StatusEvent("a1", AgentStatus.UNKNOWN, AgentStatus.ACTIVE,
                                  datetime.now(UTC)))]  # fmt: skip
    body = payload(status_events=events)
    first = push(client, token, body)
    assert first.status_code == 200
    assert first.json()["accepted"] == {
        "agents": 1,
        "status_events": 1,
        "adapter_health": 1,
        "usage": 0,
        "sessions": 0,
    }
    assert first.json()["next_push_s"] == 5
    second = push(client, token, body)
    assert second.json()["accepted"]["status_events"] == 0
    inventory = Inventory(db)
    assert [r.agent.agent_id for r in inventory.agents()] == ["a1"]
    assert len(inventory.status_history("a1")) == 1
    (health,) = inventory.adapter_health("h-pi")
    assert health.health.state is HealthState.OK


def test_requires_valid_token(client: TestClient, db: Database) -> None:
    assert push(client, None, payload()).status_code == 401
    assert push(client, "ovh_wrong", payload()).status_code == 401
    response = push(client, "ovh_wrong", payload())
    assert response.headers["www-authenticate"] == "Bearer"


def test_token_for_one_host_cannot_write_another(client: TestClient, db: Database) -> None:
    token = token_for(db, PI)
    assert push(client, token, payload(host=MAC)).status_code == 403
    sneaky = payload(agents=[to_dict(agent("a9", host=MAC))])
    assert push(client, token, sneaky).status_code == 403


def test_status_events_for_other_hosts_agents_are_dropped(client: TestClient, db: Database) -> None:
    mac_token = token_for(db, MAC)
    push(client, mac_token, payload(host=MAC, agents=[to_dict(agent("mac-agent", MAC))]))
    pi_token = token_for(db, PI)
    events = [to_dict(StatusEvent("mac-agent", AgentStatus.ACTIVE, AgentStatus.STOPPED,
                                  datetime.now(UTC)))]  # fmt: skip
    response = push(client, pi_token, payload(status_events=events))
    assert response.json()["accepted"]["status_events"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 99},
        {"sent_at": "yesterday"},
        {"agents": "nope"},
        {"agents": [{"agent_id": "a1"}]},
        {"agents": [dict(to_dict(agent()), status="exploded")]},
    ],
)
def test_malformed_payloads_rejected_without_echo(
    client: TestClient, db: Database, change: dict[str, Any]
) -> None:
    response = push(client, token_for(db, PI), payload(**change))
    assert response.status_code == 422
    assert "exploded" not in response.text and "yesterday" not in response.text


def test_too_many_items(client: TestClient, db: Database) -> None:
    body = payload(adapter_health=[to_dict(AdapterHealth(f"x{i}")) for i in range(100)])
    assert push(client, token_for(db, PI), body).status_code == 413


def test_unknown_sections_ignored_and_reported(client: TestClient, db: Database) -> None:
    response = push(client, token_for(db, PI), payload(future=[], spans=[]))
    assert response.status_code == 200
    assert response.json()["ignored"] == ["future", "spans"]


def test_clock_skew_recorded(client: TestClient, db: Database) -> None:
    skewed = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    push(client, token_for(db, PI), payload(sent_at=skewed))
    host = Inventory(db).host("h-pi")
    assert host is not None and host.clock_skew_s is not None and host.clock_skew_s > 290


def test_ingest_of_200_agents_is_fast(client: TestClient, db: Database) -> None:
    token = token_for(db, PI)
    body = payload(agents=[to_dict(agent(f"a{i}")) for i in range(200)])
    push(client, token, body)  # warm up
    start = time.perf_counter()
    assert push(client, token, body).status_code == 200
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"{elapsed:.3f}s"
