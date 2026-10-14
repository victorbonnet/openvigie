-- Audit log of every admin action (FR-17, D14): append-only and hash-chained.
-- hash = sha256(prev_hash || canonical JSON of the entry); `openvigie hub audit verify` checks it.

CREATE TABLE audit_log (
    seq INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,             -- e.g. "alice@github", "cli:root", "anonymous"
    role TEXT NOT NULL,              -- viewer | admin | local
    action TEXT NOT NULL,            -- e.g. "sandbox.policy.apply", "role.denied"
    target TEXT NOT NULL,
    before TEXT NOT NULL,            -- canonical JSON, redacted
    after TEXT NOT NULL,             -- canonical JSON, redacted
    result TEXT NOT NULL,            -- proposed | confirmed | succeeded | failed | refused | ...
    request_id TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE
);
