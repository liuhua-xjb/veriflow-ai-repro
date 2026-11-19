"""Append-only typed receipts in an independent, versioned SQLite sidecar."""
import json
import sqlite3
import threading
from pathlib import Path
from .event_store import utc
from .workflow_models import digest
from .registry import RegistryError


class VerificationStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0: db.executescript((Path(__file__).parent/'migrations/003_verification.sql').read_text())
            elif version != 1: raise RuntimeError('unsupported_verification_schema')

    def append(self, run_id, kind, **fields):
        with self.lock, sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT seq,receipt_hash FROM audit WHERE run_id=? ORDER BY seq DESC LIMIT 1',(run_id,)).fetchone()
            seq, previous = (row[0]+1,row[1]) if row else (1,'0'*64)
            payload = {'run_id':run_id,'seq':seq,'schema_version':'1.0.0','timestamp':utc(),**fields}
            receipt_hash = digest({'kind':kind,'payload':payload,'previous_hash':previous})
            db.execute('INSERT INTO audit VALUES (?,?,?,?,?,?)',(run_id,seq,kind,json.dumps(payload,allow_nan=False),previous,receipt_hash))
        return {'kind':kind, **payload, 'previous_hash':previous, 'receipt_hash':receipt_hash, 'evidence_id':f'{run_id}:{seq}:{receipt_hash}'}

    def receipts(self, run_id):
        with self.lock, sqlite3.connect(self.path) as db:
            rows = db.execute('SELECT seq,kind,payload,previous_hash,receipt_hash FROM audit WHERE run_id=? ORDER BY seq',(run_id,)).fetchall()
        return [{'kind':k,**json.loads(p),'previous_hash':h,'receipt_hash':r,'evidence_id':f'{run_id}:{s}:{r}'} for s,k,p,h,r in rows]

    def integrity(self, run_id):
        records = self.receipts(run_id); previous='0'*64
        for seq, record in enumerate(records,1):
            payload={k:v for k,v in record.items() if k not in {'kind','previous_hash','receipt_hash','evidence_id'}}
            if record['seq'] != seq or record['previous_hash'] != previous or record['receipt_hash'] != digest({'kind':record['kind'],'payload':payload,'previous_hash':previous}):
                return {'ok':False,'failed_seq':seq,'records':len(records)}
            previous=record['receipt_hash']
        return {'ok':True,'records':len(records),'head':previous,'boundary':'DB administrator can rewrite or truncate the entire chain; no external anchor.'}

    def claim(self, key, request, run_id, reserve=True):
        if key is None: return None
        key_hash, request_hash=digest(key),digest(request)
        with self.lock, sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT request_hash,run_id FROM idempotency WHERE key_hash=?',(key_hash,)).fetchone()
            if row:
                if row[0] != request_hash: raise RegistryError(409,'idempotency_conflict')
                return row[1]
            if reserve: db.execute('INSERT INTO idempotency VALUES (?,?,?)',(key_hash,request_hash,run_id))
        return None

    def recover(self, orchestration):
        with sqlite3.connect(self.path) as db:
            ids=[r[0] for r in db.execute('SELECT DISTINCT run_id FROM audit')]
        for run_id in ids:
            receipts=self.receipts(run_id)
            if any(r['kind']=='RunVerificationFinished' for r in receipts): continue
            run=orchestration.get(run_id)
            if run['state'] not in {'FAILED','CANCELLED'}: continue
            ends={r['attempt_id'] for r in receipts if r['kind']=='AttemptFinished'}
            for start in receipts:
                if start['kind']=='AttemptStarted' and start['attempt_id'] not in ends:
                    self.append(run_id,'AttemptFinished',node_id=start['node_id'],attempt_id=start['attempt_id'],status='REJECTED',reason='process_interrupted',actual_tool_completed=False)
            receipt=self.append(run_id,'RunVerificationFinished',status='REJECTED',reason='process_interrupted',tokens=0,cost=0)
            orchestration.update(run_id,'VerificationInterrupted',fields={'verification_status':'REJECTED','verification_evidence_id':receipt['evidence_id']})
