"""Append-only metadata versions, atomic compare-and-insert, SQLite schema v1."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .agents import AgentCreate, AgentRecord, AgentSpec


class RegistryError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


class AgentRegistry:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            schema = db.execute("PRAGMA user_version").fetchone()[0]
            if schema == 0:
                db.executescript((Path(__file__).parent / "migrations/001_agents.sql").read_text())
            elif schema != 1:
                raise RuntimeError("unsupported database schema; preserve DB and migrate explicitly")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, agent_id: str, version: int | None = None) -> AgentRecord:
        with self.connection() as db:
            row = self._row(db, agent_id, version)
        return AgentRecord.model_validate_json(row[0])

    @staticmethod
    def _row(db, agent_id, version=None):
        if version is None:
            row = db.execute("SELECT payload FROM agent_versions WHERE agent_id=? ORDER BY version DESC LIMIT 1", (agent_id,)).fetchone()
        else:
            row = db.execute("SELECT payload FROM agent_versions WHERE agent_id=? AND version=?", (agent_id, version)).fetchone()
        if row is None:
            raise RegistryError(404, "agent_or_version_not_found")
        return row

    def create(self, value: AgentCreate) -> AgentRecord:
        now = datetime.now(timezone.utc)
        record = AgentRecord(**value.model_dump(), created_at=now, updated_at=now)
        with self.connection() as db:
            try:
                db.execute("INSERT INTO agent_versions VALUES (?, ?, ?)", (record.id, 1, record.model_dump_json()))
            except sqlite3.IntegrityError:
                raise RegistryError(409, "agent_id_exists") from None
        return record

    def revise(self, agent_id: str, base_version: int, spec: AgentSpec | None = None,
               enabled: bool | None = None) -> AgentRecord:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = AgentRecord.model_validate_json(self._row(db, agent_id)[0])
            if previous.version != base_version:
                raise RegistryError(409, "version_conflict")
            fields = spec.model_dump() if spec else AgentSpec.model_validate(previous.model_dump(include=set(AgentSpec.model_fields))).model_dump()
            if enabled is not None:
                fields["enabled"] = enabled
            record = AgentRecord(**fields, id=agent_id, version=base_version + 1,
                                 created_at=previous.created_at, updated_at=datetime.now(timezone.utc))
            db.execute("INSERT INTO agent_versions VALUES (?, ?, ?)", (agent_id, record.version, record.model_dump_json()))
        return record

    def versions(self, agent_id: str) -> list[AgentRecord]:
        self.get(agent_id)
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM agent_versions WHERE agent_id=? ORDER BY version", (agent_id,)).fetchall()
        return [AgentRecord.model_validate_json(row[0]) for row in rows]

    def list(self, capability=None, enabled=None, tool=None) -> list[AgentRecord]:
        with self.connection() as db:
            rows = db.execute("SELECT a.payload FROM agent_versions a JOIN (SELECT agent_id, MAX(version) v FROM agent_versions GROUP BY agent_id) b ON a.agent_id=b.agent_id AND a.version=b.v ORDER BY a.agent_id").fetchall()
        values = [AgentRecord.model_validate_json(row[0]) for row in rows]
        return [a for a in values if (capability is None or capability in a.capabilities)
                and (enabled is None or enabled == a.enabled) and (tool is None or tool in a.allowed_tools)]
