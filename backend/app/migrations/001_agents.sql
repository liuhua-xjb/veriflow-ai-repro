CREATE TABLE IF NOT EXISTS agent_versions (
    agent_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    payload TEXT NOT NULL,
    PRIMARY KEY(agent_id, version)
);
PRAGMA user_version = 1;
