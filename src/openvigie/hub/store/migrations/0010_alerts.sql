-- Alerts (FR-14, D17): one row per event worth telling someone about, e.g. a budget crossing its
-- warning level or its limit in a period. `key` deduplicates; kept forever like the audit log.

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,        -- e.g. "budget:claude-daily:2026-10-14:exceeded"
    level TEXT NOT NULL,             -- warning | exceeded
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    delivered_at TEXT,               -- set once a notification channel took it
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX alerts_by_time ON alerts (at);
