-- Connections each OpenShell sandbox blocked (FR-18), counted per program and destination.

CREATE TABLE blocked_requests (
    sandbox_id TEXT NOT NULL REFERENCES openshell_sandboxes (sandbox_id) ON DELETE CASCADE,
    binary TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    count INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (sandbox_id, binary, host, port)
);
