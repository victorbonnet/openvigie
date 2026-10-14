-- Usage: one row per model call, and sessions. Token columns are NULL when unknown, never 0.

CREATE TABLE sessions (
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    session_id TEXT NOT NULL,
    started_at TEXT,
    last_event_at TEXT,
    models TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (agent_id, session_id)
);

CREATE TABLE usage_records (
    record_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    session_id TEXT,
    ts TEXT NOT NULL,
    provider TEXT,
    request_model TEXT,
    response_model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_input_tokens INTEGER,
    cache_creation_input_tokens INTEGER,
    reasoning_tokens INTEGER,
    quality TEXT NOT NULL
);
CREATE INDEX usage_records_agent_ts ON usage_records (agent_id, ts);
CREATE INDEX usage_records_model_ts ON usage_records (response_model, ts);
CREATE INDEX usage_records_ts ON usage_records (ts);

ALTER TABLE hosts ADD COLUMN spool_dropped INTEGER NOT NULL DEFAULT 0;
