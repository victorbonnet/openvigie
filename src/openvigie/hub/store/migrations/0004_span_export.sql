-- Spans waiting for export to Langfuse (#62). Not long-term storage: rows are pruned after
-- retention.span_queue_hours. Re-sent spans (same ids) replace the queued row.

CREATE TABLE span_export_queue (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    span TEXT NOT NULL,              -- model codec JSON, as validated at ingest
    received_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    exported_at TEXT,
    UNIQUE (trace_id, span_id)
);
CREATE INDEX span_export_due ON span_export_queue (exported_at, next_attempt_at);
CREATE INDEX span_export_received ON span_export_queue (received_at);

-- Traces that reached Langfuse at least once, for trace links (#63).
CREATE TABLE trace_exports (
    trace_id TEXT PRIMARY KEY,
    exported_at TEXT NOT NULL
);

CREATE TABLE hub_counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
