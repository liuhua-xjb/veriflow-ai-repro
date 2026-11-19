-- Separate orchestration.sqlite3 database; never modifies the G1 Agent DB.
CREATE TABLE workflow_versions (
 workflow_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(workflow_id, version)
);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE run_events (
 run_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(run_id, seq)
);
PRAGMA user_version=1;
