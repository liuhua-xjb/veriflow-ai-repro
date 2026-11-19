"""One immutable human-approved trial. No environment-controlled credit/renewal."""
import hashlib,sqlite3,time,math,json
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime,timezone
from .llm import ProviderError
from .event_store import utc

ROOT=Path(__file__).resolve().parents[2]
RECEIPT=ROOT/'reports/G4/deepseek/AUTHORIZATION_RECEIPT.md'
RECEIPT_SHA='f0574c5eb00502e31ab3f1992cb2492a1178977713e093417e9a76c8d459ff9b'
START=datetime.fromisoformat('2026-10-31T15:51:03+08:00').timestamp()
EXPIRES=datetime.fromisoformat('2026-11-01T15:51:03+08:00').timestamp()
POLICY={'receipt_sha256':RECEIPT_SHA,'start':START,'expires':EXPIRES,'provider':'deepseek','model':'deepseek-flash',
        'endpoint':'https://api.deepseek.com/chat/completions','requests':6,'tokens':12000,'microusd':200000,
        'output':800,'request_timeout':30,'input_rate':.3,'output_rate':1.2,'price_checked':'2026-10-31'}

def authorization(now=None):
    if not RECEIPT.is_file() or hashlib.sha256(RECEIPT.read_bytes()).hexdigest()!=RECEIPT_SHA:raise ProviderError('AUTHORIZATION_RECEIPT_MISMATCH')
    timestamp=time.time() if now is None else now
    if not START<=timestamp<EXPIRES:raise ProviderError('AUTHORIZATION_EXPIRED_OR_NOT_STARTED')
    return POLICY

class RealBudgetLedger:
    def __init__(self,path,mode='REAL_LLM',clock=time.time):
        if mode not in {'REAL_LLM','SIMULATED'}:raise ValueError('invalid_mode')
        self.path=Path(path);self.mode=mode;self.clock=clock;self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)')
            payload=json.dumps({'mode':mode,**POLICY},sort_keys=True)
            row=db.execute('SELECT payload FROM policy WHERE id=1').fetchone()
            if row and row[0]!=payload:raise ProviderError('LEDGER_NAMESPACE_OR_POLICY_MISMATCH')
            db.execute('INSERT OR IGNORE INTO policy VALUES (1,?)',(payload,))
            db.execute('CREATE TABLE IF NOT EXISTS slots (run_id TEXT,role TEXT,input_bound INTEGER,tokens INTEGER,microusd INTEGER,status TEXT,request_hash TEXT,payload TEXT,PRIMARY KEY(run_id,role))')
            db.execute('CREATE TABLE IF NOT EXISTS journal (seq INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            for op in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS journal_no_{op} BEFORE {op} ON journal BEGIN SELECT RAISE(ABORT,'append_only'); END")
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db:yield db
        finally:db.close()
    def check(self):authorization(self.clock())
    def event(self,db,**fields):
        db.execute('INSERT INTO journal(payload) VALUES (?)',(json.dumps({'timestamp':utc(),'mode':self.mode,**fields},allow_nan=False),))
    def can_fit(self,bounds):
        self.check()
        if any(type(v) is not int or not 0<v<=4000 for v in bounds):raise ProviderError('INPUT_BOUND_INVALID')
        with self.db() as db:
            count,tokens,cost=db.execute('SELECT COUNT(*),COALESCE(SUM(tokens),0),COALESCE(SUM(microusd),0) FROM slots').fetchone()
        if count+len(bounds)>6 or tokens+sum(v+800 for v in bounds)>12000 or cost+sum(math.ceil(v*.3+800*1.2) for v in bounds)>200000:raise ProviderError('BUDGET_EXHAUSTED')
    def reserve_cycle(self,run_id,planner_bound,reviewer_bound=4000):
        self.check()
        if any(type(v) is not int or not 0<v<=4000 for v in (planner_bound,reviewer_bound)):raise ProviderError('INPUT_BOUND_INVALID')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.check()
            if db.execute('SELECT 1 FROM slots WHERE run_id=?',(run_id,)).fetchone():raise ProviderError('CYCLE_ALREADY_RESERVED_NO_RETRY')
            count,tokens,cost=db.execute('SELECT COUNT(*),COALESCE(SUM(tokens),0),COALESCE(SUM(microusd),0) FROM slots').fetchone()
            bounds=[planner_bound,reviewer_bound];charges=[math.ceil(v*.3+800*1.2) for v in bounds]
            if count+2>6 or tokens+sum(v+800 for v in bounds)>12000 or cost+sum(charges)>200000:raise ProviderError('BUDGET_EXHAUSTED')
            for role,bound,charge in zip(('planner','reviewer'),bounds,charges):
                db.execute('INSERT INTO slots VALUES (?,?,?,?,?,?,?,?)',(run_id,role,bound,bound+800,charge,'HELD',None,'{}'))
                self.event(db,run_id=run_id,role=role,status='HELD',tokens_reserved=bound+800,microusd_reserved=charge)
    def dispatch(self,run_id,role,request_hash,input_bound):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.check()
            row=db.execute('SELECT input_bound,status FROM slots WHERE run_id=? AND role=?',(run_id,role)).fetchone()
            if not row or row[1]!='HELD':raise ProviderError('ALREADY_DISPATCHED_OR_NOT_RESERVED')
            if input_bound>row[0]:raise ProviderError('INPUT_RESERVATION_EXCEEDED')
            db.execute("UPDATE slots SET status='SENT_OR_UNKNOWN',request_hash=? WHERE run_id=? AND role=?",(request_hash,run_id,role))
            self.event(db,run_id=run_id,node_id=role,attempt_id=role+':1',request_digest=request_hash,status='SENT_OR_UNKNOWN')
    def finish(self,run_id,role,status,payload):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute("UPDATE slots SET status=?,payload=? WHERE run_id=? AND role=? AND status='SENT_OR_UNKNOWN'",(status,json.dumps(payload),run_id,role)).rowcount
            if changed!=1:raise ProviderError('INVALID_SETTLEMENT')
            self.event(db,**{**payload,'run_id':run_id,'role':role,'status':status})
    def snapshot(self):
        with self.db() as db:
            rows=db.execute('SELECT run_id,role,input_bound,tokens,microusd,status,payload FROM slots ORDER BY rowid').fetchall()
            journal=[json.loads(r[0]) for r in db.execute('SELECT payload FROM journal ORDER BY seq')]
        reserved_tokens=sum(r[3] for r in rows);reserved_cost=sum(r[4] for r in rows)
        return {'mode':self.mode,'authorization_expires_at':'2026-11-01T15:51:03+08:00','requests_reserved':len(rows),
                'sent_or_unknown':sum(r[5]!='HELD' for r in rows),'confirmed_responses':sum(json.loads(r[6]).get('official_response_received',False) for r in rows),
                'tokens_reserved':reserved_tokens,'usd_reserved':reserved_cost/1e6,'requests_remaining':6-len(rows),'tokens_remaining':12000-reserved_tokens,
                'usd_remaining':(200000-reserved_cost)/1e6,'supplier_actual_bill_usd':None,'journal':journal}
