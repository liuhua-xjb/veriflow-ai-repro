"""Physical independent ledger. No releases, refunds, policy updates or retries."""
import json,sqlite3,time,re
from contextlib import contextmanager
from pathlib import Path
from .g4r3_policy import ROOT,ID,POLICY,authorization,bounds,dryrun,progression_review
from .g4r3_history import verify as verify_history
from .deepseek_repeat_prep import micro_usd
from .event_store import utc
from .llm import ProviderError

class G4R3Ledger:
    def __init__(self,path,mode='REAL_LLM',clock=time.time):
        self.path=Path(path).resolve();self.mode=mode;self.clock=clock
        prod=ROOT/'runtime/deepseek_g4r3.sqlite3'
        if mode not in {'REAL_LLM','SIMULATED'} or not (self.path.is_relative_to(ROOT/'.cache') or mode=='REAL_LLM' and self.path==prod):raise ProviderError('G4R3_LEDGER_PATH_REJECTED')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and not {'policy','units','slots','journal','control'}<=tables:raise ProviderError('G4R3_LEDGER_NAMESPACE_MISMATCH')
            payload=json.dumps({'mode':mode,**POLICY},sort_keys=True)
            if 'policy' in tables:
                row=db.execute('SELECT payload FROM policy WHERE id=1').fetchone()
                if not row or row[0]!=payload:raise ProviderError('G4R3_LEDGER_NAMESPACE_MISMATCH')
            db.executescript('CREATE TABLE IF NOT EXISTS policy(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);'
                'CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),stop_reason TEXT);'
                'CREATE TABLE IF NOT EXISTS units(unit_key TEXT PRIMARY KEY,run_id TEXT UNIQUE,state TEXT,reason TEXT);'
                'CREATE TABLE IF NOT EXISTS slots(seq INTEGER PRIMARY KEY,run_id TEXT,role TEXT,input_bound INTEGER,tokens INTEGER,microusd INTEGER,status TEXT,request_hash TEXT,payload TEXT,UNIQUE(run_id,role));'
                'CREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY,payload TEXT NOT NULL);')
            db.execute('INSERT OR IGNORE INTO policy VALUES(1,?)',(payload,));db.execute('INSERT OR IGNORE INTO control VALUES(1,NULL)')
            for table in ('policy','journal'):
                for op in ('UPDATE','DELETE'):db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable'); END")
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=15)
        try:
            with db:yield db
        finally:db.close()
    def check(self):authorization(self.clock());dryrun()
    def assert_bound(self,mode):
        with self.db() as db:row=db.execute('SELECT payload FROM policy WHERE id=1').fetchone()
        if self.mode!=mode or not row or row[0]!=json.dumps({'mode':mode,**POLICY},sort_keys=True):raise ProviderError('G4R3_LEDGER_NAMESPACE_MISMATCH')
    def event(self,db,**fields):db.execute('INSERT INTO journal(payload) VALUES(?)',(json.dumps({'utc':utc(),'authorization_id':ID,'mode':self.mode,**fields},allow_nan=False),))
    def reserve(self,key,run_id):
        self.check();self.assert_bound(self.mode);verify_history();b=bounds(key)
        if not re.fullmatch('run_g4r3_[a-f0-9]{32}',run_id):raise ProviderError('G4R3_RUN_NAMESPACE_REQUIRED')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.check()
            if db.execute('SELECT stop_reason FROM control').fetchone()[0]:raise ProviderError('G4R3_BATCH_STOPPED')
            if db.execute('SELECT 1 FROM units WHERE unit_key=?',(key,)).fetchone():raise ProviderError('G4R3_UNIT_ALREADY_CLAIMED')
            if db.execute("SELECT 1 FROM units WHERE state='RUNNING'").fetchone():raise ProviderError('G4R3_BATCH_BUSY')
            if key in {'main:45','main:46'}:
                prior=db.execute("SELECT run_id,state FROM units WHERE unit_key='main:44'").fetchone()
                if not prior or prior[1]!='SUCCEEDED':raise ProviderError('G4R3_SEQUENTIAL_MAIN_REQUIRED')
                if self.mode=='REAL_LLM':progression_review(prior[0])
                if key=='main:46' and db.execute("SELECT state FROM units WHERE unit_key='main:45'").fetchone()!=('SUCCEEDED',):raise ProviderError('G4R3_SEQUENTIAL_MAIN_REQUIRED')
            if key.startswith('extra:') and db.execute("SELECT COUNT(*) FROM units WHERE unit_key LIKE 'main:%' AND state='SUCCEEDED'").fetchone()[0]!=3:raise ProviderError('G4R3_MAIN_QUEUE_NOT_COMPLETE')
            n,t,c=db.execute('SELECT COUNT(*),COALESCE(SUM(tokens),0),COALESCE(SUM(microusd),0) FROM slots').fetchone()
            if n+len(b)>8 or t+sum(v+800 for v in b.values())>32000 or c+sum(micro_usd(v) for v in b.values())>30000:raise ProviderError('G4R3_BUDGET_EXHAUSTED')
            db.execute('INSERT INTO units VALUES(?,?,?,NULL)',(key,run_id,'RUNNING'))
            for role,v in b.items():
                db.execute('INSERT INTO slots(run_id,role,input_bound,tokens,microusd,status,payload) VALUES(?,?,?,?,?,?,?)',(run_id,role,v,v+800,micro_usd(v),'HELD','{}'))
                self.event(db,kind='HELD',unit_key=key,run_id=run_id,role=role,input_bound=v,tokens=v+800,microusd=micro_usd(v))
    def context(self,run_id):
        with self.db() as db:row=db.execute('SELECT unit_key,state FROM units WHERE run_id=?',(run_id,)).fetchone()
        if not row or row[1]!='RUNNING':raise ProviderError('G4R3_NO_ACTIVE_RESERVATION')
        return row[0]
    def slot(self,run_id,role):
        with self.db() as db:row=db.execute('SELECT input_bound,status,request_hash FROM slots WHERE run_id=? AND role=?',(run_id,role)).fetchone()
        if not row:raise ProviderError('G4R3_NO_ROLE_RESERVATION')
        return row
    def dispatch(self,run_id,role,request_hash,bound):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.check()
            active=db.execute('SELECT state FROM units WHERE run_id=?',(run_id,)).fetchone()
            row=db.execute('SELECT input_bound,status FROM slots WHERE run_id=? AND role=?',(run_id,role)).fetchone()
            if not active or active[0]!='RUNNING' or not row or row[1]!='HELD' or bound>row[0]:raise ProviderError('G4R3_DISPATCH_REJECTED')
            if role=='reviewer' and db.execute("SELECT status FROM slots WHERE run_id=? AND role='planner'",(run_id,)).fetchone()!=('COMPLETED',):raise ProviderError('G4R3_REVIEW_BEFORE_PLAN')
            db.execute("UPDATE slots SET status='SENT_OR_UNKNOWN',request_hash=? WHERE run_id=? AND role=?",(request_hash,run_id,role))
            self.event(db,kind='SENT_OR_UNKNOWN',run_id=run_id,node_id=role,attempt_id=role+':1',request_digest=request_hash)
    def settle(self,run_id,role,status,payload):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if status not in {'COMPLETED','FAILED'}:raise ProviderError('G4R3_INVALID_SETTLEMENT')
            n=db.execute("UPDATE slots SET status=?,payload=? WHERE run_id=? AND role=? AND status='SENT_OR_UNKNOWN'",(status,json.dumps(payload,allow_nan=False),run_id,role)).rowcount
            if n!=1:raise ProviderError('G4R3_INVALID_SETTLEMENT')
            self.event(db,**{**payload,'kind':status,'run_id':run_id,'role':role})
    def finish(self,run_id,status,reason=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if status=='SUCCEEDED' and db.execute("SELECT COUNT(*) FROM slots WHERE run_id=? AND status!='COMPLETED'",(run_id,)).fetchone()[0]:raise ProviderError('G4R3_INCOMPLETE_UNIT')
            n=db.execute("UPDATE units SET state=?,reason=? WHERE run_id=? AND state='RUNNING'",(status,reason,run_id)).rowcount
            if n!=1:raise ProviderError('G4R3_UNIT_TERMINAL')
            if status!='SUCCEEDED':db.execute('UPDATE control SET stop_reason=? WHERE id=1',(reason or status,))
            self.event(db,kind='UNIT_FINISHED',run_id=run_id,status=status,reason=reason)
    def recover(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for (run_id,) in db.execute("SELECT run_id FROM units WHERE state='RUNNING'").fetchall():
                db.execute("UPDATE units SET state='ABANDONED',reason='PROCESS_INTERRUPTED' WHERE run_id=?",(run_id,))
                db.execute("UPDATE control SET stop_reason='PROCESS_INTERRUPTED' WHERE id=1")
                self.event(db,kind='PROCESS_INTERRUPTED',run_id=run_id)
    def snapshot(self):
        with self.db() as db:
            n,t,c=db.execute('SELECT COUNT(*),COALESCE(SUM(tokens),0),COALESCE(SUM(microusd),0) FROM slots').fetchone()
            sent=db.execute("SELECT COUNT(*) FROM slots WHERE status!='HELD'").fetchone()[0]
            units=[{'unit_key':k,'run_id':r,'state':s,'reason':e} for k,r,s,e in db.execute('SELECT * FROM units ORDER BY rowid')]
            journal=[json.loads(p) for (p,) in db.execute('SELECT payload FROM journal ORDER BY seq')]
            stop=db.execute('SELECT stop_reason FROM control').fetchone()[0]
        return {'authorization_id':ID,'mode':self.mode,'expires_at':'2026-11-09T13:42:16+08:00','requests_reserved':n,'sent_or_unknown':sent,
            'tokens_reserved':t,'usd_reserved':c/1e6,'requests_remaining':8-n,'tokens_remaining':32000-t,'usd_remaining':(30000-c)/1e6,
            'units':units,'stop_reason':stop,'journal':journal,'supplier_actual_bill_usd':None}
