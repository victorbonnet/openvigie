-- Commands for collectors (D14): queued by a confirmed admin action, delivered once in the
-- ingest reply of the target host, answered by the collector in a later push.

CREATE TABLE commands (
    command_id TEXT PRIMARY KEY,     -- 32 hex characters
    host_id TEXT NOT NULL,
    action TEXT NOT NULL,
    args TEXT NOT NULL,              -- JSON object of strings
    actor TEXT NOT NULL,             -- who confirmed it
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,            -- queued | delivered | expired | succeeded | failed | refused
    delivered_at TEXT,
    finished_at TEXT,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX commands_by_host ON commands (host_id, status);
