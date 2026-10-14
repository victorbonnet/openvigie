-- Inventory: hosts, agents, status history, adapter health, collector credentials.
-- Timestamps are UTC ISO 8601 strings with a fixed width (see store.ts), so they sort lexically.

CREATE TABLE hosts (
    host_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    os TEXT NOT NULL,
    arch TEXT NOT NULL,
    collector_version TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    clock_skew_s REAL,
    revoked_at TEXT
);

CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES hosts (host_id),
    agent_type TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT,
    config_root TEXT,
    sandboxed TEXT NOT NULL,
    sandbox_kind TEXT,
    process_running INTEGER,
    last_activity_at TEXT,
    status TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX agents_host ON agents (host_id);

CREATE TABLE agent_status_events (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    old TEXT NOT NULL,
    new TEXT NOT NULL,
    at TEXT NOT NULL,
    UNIQUE (agent_id, at, new)
);
CREATE INDEX agent_status_events_agent_at ON agent_status_events (agent_id, at);

CREATE TABLE adapter_health (
    host_id TEXT NOT NULL REFERENCES hosts (host_id),
    adapter TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    parse_errors INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (host_id, adapter)
);

CREATE TABLE host_tokens (
    token_hash TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES hosts (host_id),
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX host_tokens_host ON host_tokens (host_id);

CREATE TABLE enrol_codes (
    code_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    bound_host_id TEXT
);
