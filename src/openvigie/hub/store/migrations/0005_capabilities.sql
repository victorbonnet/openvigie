-- What each agent can access (FR-8): the latest snapshot, and a log of what changed.

CREATE TABLE agent_capabilities (
    agent_id TEXT PRIMARY KEY REFERENCES agents (agent_id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    snapshot TEXT NOT NULL,          -- model codec JSON
    digest TEXT NOT NULL
);

CREATE TABLE capability_changes (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (agent_id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    field TEXT NOT NULL,
    change TEXT NOT NULL,            -- added | removed | changed
    detail TEXT NOT NULL
);
CREATE INDEX capability_changes_agent ON capability_changes (agent_id, at);
