CREATE TABLE tester_users (
    did TEXT PRIMARY KEY,
    identity_document TEXT,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'disabled')),
    requested_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    last_login_at TEXT
);
CREATE INDEX tester_users_status_requested_idx ON tester_users(status, requested_at);
