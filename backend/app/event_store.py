import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .registry import RegistryError

TERMINAL = {'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED'}
TRANSITIONS = {'PENDING': {'READY', 'RUNNING', 'FAILED', 'SKIPPED', 'CANCELLED'},
               'READY': {'RUNNING', 'FAILED', 'SKIPPED', 'CANCELLED'},
               'RUNNING': {'SUCCEEDED', 'FAILED', 'CANCELLED'}}


def utc():
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                db.executescript((Path(__file__).parent / 'migrations/002_orchestration.sql').read_text())
            elif version != 1:
                raise RuntimeError('unsupported orchestration schema')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db: yield db
        finally: db.close()

    def workflow(self, workflow_id, version=None):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM workflow_versions WHERE workflow_id=? ' +
                ('ORDER BY version DESC LIMIT 1' if version is None else 'AND version=?'),
                (workflow_id,) if version is None else (workflow_id, version)).fetchone()
        if not row: raise RegistryError(404, 'workflow_not_found')
        return json.loads(row[0])

    def workflows(self):
        with self.connection() as db:
            rows = db.execute('SELECT w.payload FROM workflow_versions w JOIN (SELECT workflow_id,MAX(version) v FROM workflow_versions GROUP BY workflow_id) m ON w.workflow_id=m.workflow_id AND w.version=m.v ORDER BY w.workflow_id').fetchall()
        return [json.loads(r[0]) for r in rows]

    def versions(self, workflow_id):
        self.workflow(workflow_id)
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM workflow_versions WHERE workflow_id=? ORDER BY version', (workflow_id,))]

    def save_workflow(self, workflow_id, spec, base_version=None):
        with self.lock, self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT MAX(version) FROM workflow_versions WHERE workflow_id=?', (workflow_id,)).fetchone()[0]
            if base_version is None and current is not None: raise RegistryError(409, 'workflow_exists')
            if base_version is not None and current is None: raise RegistryError(404, 'workflow_not_found')
            if base_version is not None and current != base_version: raise RegistryError(409, 'workflow_version_conflict')
            payload = {**spec.model_dump(mode='json'), 'workflow_id': workflow_id, 'version': (current or 0) + 1, 'created_at': utc()}
            db.execute('INSERT INTO workflow_versions VALUES (?,?,?)', (workflow_id, payload['version'], json.dumps(payload)))
        return payload

    def create_run(self, run):
        with self.lock, self.connection() as db:
            db.execute('INSERT INTO runs VALUES (?,?)', (run['run_id'], json.dumps(run)))
            self._event(db, run, 'RunCreated', None, None, 'PENDING', {})

    @staticmethod
    def _event(db, run, kind, node_id, previous, state, metadata):
        seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM run_events WHERE run_id=?', (run['run_id'],)).fetchone()[0]
        event = {'seq': seq, 'timestamp': utc(), 'run_id': run['run_id'], 'node_id': node_id,
                 'attempt_id': metadata.get('attempt_id', run['nodes'][node_id].get('attempt_id', node_id + ':1')) if node_id else None, 'kind': kind,
                 'from_state': previous, 'to_state': state, 'metadata': metadata}
        db.execute('INSERT INTO run_events VALUES (?,?,?)', (run['run_id'], seq, json.dumps(event)))

    def get(self, run_id):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if not row: raise RegistryError(404, 'run_not_found')
        return json.loads(row[0])

    def runs(self):
        with self.connection() as db:
            rows = db.execute('SELECT payload FROM runs ORDER BY rowid DESC LIMIT 100').fetchall()
        return [json.loads(r[0]) for r in rows]

    def update(self, run_id, kind, node_id=None, state=None, fields=None, metadata=None):
        with self.lock, self.connection() as db:
            run = json.loads(db.execute('SELECT payload FROM runs WHERE run_id=?', (run_id,)).fetchone()[0])
            target = run['nodes'][node_id] if node_id else run
            old = target['state']
            if state is not None and state != old:
                if state not in TRANSITIONS.get(old, set()): raise RuntimeError('illegal_state_transition')
                target['state'] = state
            target.update(fields or {})
            self._event(db, run, kind, node_id, old, target['state'], metadata or {})
            db.execute('UPDATE runs SET payload=? WHERE run_id=?', (json.dumps(run), run_id))
        return run

    def events(self, run_id, after=0, limit=100):
        self.get(run_id)
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?', (run_id, after, limit))]

    def interrupt_orphans(self):
        after = ''
        while True:
            with self.connection() as db:
                rows = db.execute('SELECT run_id,payload FROM runs WHERE run_id>? ORDER BY run_id LIMIT 100',(after,)).fetchall()
            if not rows: break
            after = rows[-1][0]
            for _, payload in rows:
                run = json.loads(payload)
                if run['state'] not in TERMINAL:
                    for node_id, node in run['nodes'].items():
                        if node['state'] not in TERMINAL:
                            self.update(run['run_id'], 'ProcessInterrupted', node_id, 'FAILED', {'error': 'process_interrupted'})
                    self.update(run['run_id'], 'ProcessInterrupted', state='FAILED', fields={'error': 'process_interrupted', 'finished_at': utc()})
