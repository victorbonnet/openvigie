-- Write actions (FR-17, D14): a proposal with its diff, then an admin's typed confirmation,
-- which queues a command. `before` keeps what is needed to roll the change back.

CREATE TABLE actions (
    action_id TEXT PRIMARY KEY,      -- 32 hex characters
    kind TEXT NOT NULL,              -- e.g. "noop", later "sandbox.policy"
    host_id TEXT NOT NULL,
    confirm_name TEXT NOT NULL,      -- what the admin must type to confirm
    command TEXT NOT NULL,           -- the collector action to run
    args TEXT NOT NULL,              -- JSON object of strings
    summary TEXT NOT NULL,
    before TEXT NOT NULL,            -- the current state, as shown in the diff
    after TEXT NOT NULL,             -- the state after the change
    rollback_of TEXT,                -- the action this one undoes
    proposed_by TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,        -- a proposal not confirmed by then can't be
    status TEXT NOT NULL,            -- proposed | confirmed | cancelled | expired
    decided_by TEXT,
    decided_at TEXT,
    command_id TEXT                  -- set once confirmed
);
CREATE INDEX actions_by_time ON actions (proposed_at);
