CREATE TABLE tester_reports (
    project TEXT NOT NULL,
    run TEXT NOT NULL,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    tests INTEGER,
    passed INTEGER,
    failed INTEGER,
    skipped INTEGER,
    storage_path TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (project, run)
);
CREATE INDEX tester_reports_finished_idx ON tester_reports(finished_at DESC);
