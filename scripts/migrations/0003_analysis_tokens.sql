CREATE TABLE tester_analysis_tokens (
    id TEXT PRIMARY KEY,
    digest TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES tester_users(did),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
)
