-- How far each agent's usage logs have been read (FR-3): while a collector is still reading
-- history, usage after read_until may be missing and totals are shown as incomplete.

CREATE TABLE usage_backfill (
    agent_id TEXT PRIMARY KEY REFERENCES agents (agent_id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    complete INTEGER NOT NULL,
    read_until TEXT,                 -- log files last written before this are all read
    bytes_left INTEGER
);
