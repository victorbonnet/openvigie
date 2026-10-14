-- OpenShell sandboxes per host (FR-10): the latest snapshot, and a log of what changed.

CREATE TABLE openshell_sandboxes (
    sandbox_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES hosts (host_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    snapshot TEXT NOT NULL,          -- model codec JSON
    digest TEXT NOT NULL
);
CREATE INDEX openshell_sandboxes_host ON openshell_sandboxes (host_id);

-- Kept after a sandbox is deleted: the history is the point.
CREATE TABLE sandbox_changes (
    id INTEGER PRIMARY KEY,
    sandbox_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    name TEXT NOT NULL,
    at TEXT NOT NULL,
    change TEXT NOT NULL,            -- created | deleted | added | removed | changed
    detail TEXT NOT NULL
);
CREATE INDEX sandbox_changes_sandbox ON sandbox_changes (sandbox_id, at);
