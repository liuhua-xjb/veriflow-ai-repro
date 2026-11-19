"""G4R3 Phase P. Synthetic tokens, loopback HTTP and isolated DBs only."""
import asyncio,copy,json,sqlite3,time,hashlib
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from uuid import uuid4
from types import SimpleNamespace
import httpx,pytest
from test_deepseek import local
from app import g4r3_policy as policy
from app.g4r3_ledger import G4R3Ledger
from app.g4r3_approval import R3ApprovalSession,HEADER
from app.g4r3_provider import G4R3Provider,G4R3SimulatedProvider
from app.g4r3_service import G4R3Service
from app.deepseek_repeat_prep import RepeatUnit,repeat_body
from app.deepseek_contracts import PLANNER_EXAMPLE
from app.llm import ProviderError

def rid():return 'run_g4r3_'+uuid4().hex
def ledger(path,mode='SIMULATED'):return G4R3Ledger(path,mode,clock=lambda:policy.START+60)
def session():return R3ApprovalSession.issue(wall=lambda:policy.START+60)
def real_provider(l,state,monkeypatch):
    # TEST_ONLY module constant replacement; no environment or API bypass switch.
    monkeypatch.setattr('app.g4r3_provider.PRODUCTION_LEDGER',l.path)
    return G4R3Provider(l,state)
def headers(token=None,**extra):return {'Host':'127.0.0.1:18000',**({HEADER:token} if token else {}),**extra}
def plan(seed):
    return {**copy.deepcopy(PLANNER_EXAMPLE),'seed':seed,'models':['dummy','logistic','random_forest'],'reason':'balanced_comparison',
        'tools':['data_profiler','leakage_check','model_trainer','model_evaluator','report_exporter']}
def completed(l,key):
    r=rid();l.reserve(key,r)
    for role,b in policy.bounds(key).items():l.dispatch(r,role,'TEST_ONLY',b);l.settle(r,role,'COMPLETED',{'mode':'SIMULATED','official_response_received':False})
    l.finish(r,'SUCCEEDED');return r

@pytest.fixture(autouse=True)
def forbid_paid_transport(monkeypatch):
    original=httpx.AsyncClient.send
    async def send(self,request,*a,**kw):
        assert request.url.scheme=='http' and request.url.host=='127.0.0.1','NO_OFFICIAL_HTTP_IN_PHASE_P'
        return await original(self,request,*a,**kw)
    monkeypatch.setattr(httpx.AsyncClient,'send',send)

@pytest.mark.parametrize('now',[policy.START-1,policy.EXPIRES,policy.EXPIRES+1])
def test_r3_absolute_authorization_boundaries(now):
    with pytest.raises(ProviderError):policy.authorization(now)
def test_r3_start_inclusive_and_receipt_sha(tmp_path,monkeypatch):
    assert policy.authorization(policy.START)['authorization_id']==policy.ID
    p=tmp_path/'receipt.md';p.write_text('TEST_ONLY forged receipt',encoding='utf-8')
    monkeypatch.setattr(policy,'RECEIPT',p)
    with pytest.raises(ProviderError,match='RECEIPT_MISMATCH'):policy.authorization(policy.START)
def test_r3_wrong_matrix_rejected(tmp_path,monkeypatch):
    p=tmp_path/'matrix.md';p.write_text('TEST_ONLY',encoding='utf-8');monkeypatch.setattr(policy,'MATRIX',p)
    with pytest.raises(ProviderError,match='MATRIX_MISMATCH'):policy.authorization(policy.START)

def test_r3_policy_old_ledger_and_mode_separation(tmp_path):
    old=policy.ROOT/'runtime/deepseek_real_trial.sqlite3';before=hashlib.sha256(old.read_bytes()).hexdigest()
    with pytest.raises(ProviderError):G4R3Ledger(old)
    l=ledger(tmp_path/'r2.sqlite3')
    with pytest.raises(ProviderError):ledger(l.path,'REAL_LLM')
    assert hashlib.sha256(old.read_bytes()).hexdigest()==before
    with l.db() as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE policy SET payload='fake'")

def test_r3_revised_budget_and_same_intent():
    d=policy.dryrun();main=d['units'][:3];extra=d['units'][3:]
    assert {r['intent'] for r in main}=={'balanced_comparison'} and [r['seed'] for r in main]==list(range(44,47))
    assert sum(r['requests'] for r in main)==6 and sum(r['tokens'] for r in main)==23934
    assert all(set(r['input_bounds'])=={'planner'} for r in extra)
    assert d['totals']=={'requests':8,'tokens':30954,'microusd':15049}
def test_r3_schema_growth_stops_before_spend(monkeypatch,tmp_path):
    monkeypatch.setattr(policy,'input_bound',lambda body:4001)
    with pytest.raises(ProviderError,match='INPUT_POLICY'):ledger(tmp_path/'x.sqlite3').reserve('main:44',rid())

def race_reserve(path):
    try:ledger(Path(path)).reserve('main:44',rid());return True
    except ProviderError:return False
def test_r3_real_multiprocess_reservation_race(tmp_path):
    import multiprocessing
    l=ledger(tmp_path/'race.sqlite3')
    with ProcessPoolExecutor(3,mp_context=multiprocessing.get_context('spawn')) as pool:
        assert sum(pool.map(race_reserve,[str(l.path)]*3))==1
    assert l.snapshot()['requests_reserved']==2

def test_r3_restart_no_credit_refresh_no_retry(tmp_path):
    path=tmp_path/'restart.sqlite3';l=ledger(path);r=rid();l.reserve('main:44',r);l.dispatch(r,'planner','h',2713)
    reopened=ledger(path)
    with pytest.raises(ProviderError):reopened.dispatch(r,'planner','h',2713)
    before=reopened.snapshot();reopened.recover();after=reopened.snapshot()
    assert before['tokens_reserved']==after['tokens_reserved']==7978 and after['stop_reason']=='PROCESS_INTERRUPTED'
    with pytest.raises(ProviderError,match='BATCH_STOPPED'):reopened.reserve('main:44',rid())

@pytest.mark.parametrize('kind',['tokens_exact','tokens_one_over','cost_exact','cost_one_over','requests'])
def test_r3_budget_last_token_cost_and_request_boundary(tmp_path,kind):
    l=ledger(tmp_path/'limit.sqlite3');b=policy.bounds('main:44');tokens=sum(v+800 for v in b.values())
    from app.deepseek_repeat_prep import micro_usd
    cost=sum(micro_usd(v) for v in b.values())
    with l.db() as db:
        for i in range(7 if kind=='requests' else 1):
            t=32000-tokens+(1 if kind=='tokens_one_over' else 0) if kind.startswith('tokens') else 0
            c=30000-cost+(1 if kind=='cost_one_over' else 0) if kind.startswith('cost') else 0
            db.execute('INSERT INTO slots(run_id,role,input_bound,tokens,microusd,status,payload) VALUES(?,?,?,?,?,?,?)',('TEST_ONLY_'+str(i),'planner',1,t,c,'FAILED','{}'))
    if kind.endswith('exact'):
        l.reserve('main:44',rid());s=l.snapshot();assert s['tokens_remaining']==0 if kind.startswith('tokens') else s['usd_remaining']==0
    else:
        before=l.snapshot()
        with pytest.raises(ProviderError,match='BUDGET_EXHAUSTED'):l.reserve('main:44',rid())
        assert before==l.snapshot()

def test_r3_main_once_extras_only_after_history_two_plus_three_and_no_reviewer(tmp_path):
    l=ledger(tmp_path/'queues.sqlite3')
    with pytest.raises(ProviderError,match='MAIN_QUEUE_NOT_COMPLETE'):l.reserve('extra:latency',rid())
    for seed in range(44,47):completed(l,f'main:{seed}')
    completed(l,'extra:recall_quality');r=completed(l,'extra:latency')
    assert l.snapshot()['requests_reserved']==8 and l.snapshot()['tokens_reserved']==30954
    with pytest.raises(ProviderError):l.reserve('main:44',rid())
    with pytest.raises(ProviderError):l.slot(r,'reviewer')

@pytest.mark.parametrize('route',['/api/g4r3/main','/api/g4r3/extra','/api/g4r3/run_g4r3_test/cancel'])
@pytest.mark.parametrize('case',['missing','wrong','expired','old_process','wrong_host','wrong_origin','null_origin','duplicate_token','duplicate_host','duplicate_origin','query','token_in_body'])
def test_r3_http_auth_denials_before_any_service(client,route,case):
    s,token=session();client.app.state.g4r3_approval=s;h=headers(token);body={}
    class Stub:
        calls=0
        async def start(self,key):self.calls+=1;return {}
        def cancel(self,key):self.calls+=1;return {}
    stub=Stub();client.app.state.g4r3_service=stub
    if case=='missing':h.pop(HEADER)
    if case=='wrong':h[HEADER]='0'*64
    if case=='expired':s._deadline=0
    if case=='old_process':client.app.state.g4r3_approval=session()[0]
    if case=='wrong_host':h['Host']='evil.example:18000'
    if case=='wrong_origin':h['Origin']='https://evil.example'
    if case=='null_origin':h['Origin']='null'
    if case=='query':route+='?token=not-a-credential'
    if case=='token_in_body':h.pop(HEADER);body['approval']=token
    if case.startswith('duplicate'):
        key={'duplicate_token':HEADER,'duplicate_host':'Host','duplicate_origin':'Origin'}[case]
        if key=='Origin':h['Origin']='http://127.0.0.1:15173'
        h=list(h.items())+[(key,h[key])]
    response=client.post(route,headers=h,json=body)
    assert response.status_code in {401,403} and stub.calls==0 and client.app.state.orchestration.runs()==[]
    assert token not in response.text

def test_r3_legacy_session_cannot_authorize_new_round(client):
    from app.deepseek_approval import ApprovalSession
    s,token=ApprovalSession.issue();client.app.state.g4r3_approval=s
    assert client.post('/api/g4r3/main',headers=headers(token),json={'seed':44}).status_code==401
def test_r3_token_cleanup_and_later_absolute_deadline():
    s,t=R3ApprovalSession.issue(wall=lambda:policy.EXPIRES-1)
    s.authenticate(t);s.close()
    with pytest.raises(ProviderError):s.authenticate(t)

def test_r3_prekey_gate_before_key_read_or_ledger(client,tmp_path,monkeypatch):
    import os
    s,t=session();client.app.state.g4r3_approval=s;l=ledger(tmp_path/'real.sqlite3','REAL_LLM');p=real_provider(l,client.app.state,monkeypatch)
    client.app.state.g4r3_service=G4R3Service(client.app.state,l,p)
    monkeypatch.setattr('app.g4r3_provider.phase_gate',lambda:(_ for _ in ()).throw(ProviderError('AWAITING_CHATGPT_G4R3_PREKEY_REVIEW')))
    original=os._Environ.get
    def get(env,key,*a):
        assert key not in {'VERIFLOW_G4R3_API_KEY','VERIFLOW_LLM_API_KEY'},'PHASE_P_KEY_READ'
        return original(env,key,*a)
    monkeypatch.setattr(os._Environ,'get',get)
    response=client.post('/api/g4r3/main',headers=headers(t),json={'seed':44})
    assert response.status_code==403 and response.json()['error']['code']=='AWAITING_CHATGPT_G4R3_PREKEY_REVIEW'
    assert l.snapshot()['requests_reserved']==0 and client.app.state.orchestration.runs()==[]

@pytest.mark.parametrize('method',['prepare','secret','complete','exchange'])
def test_r3_direct_provider_calls_require_r3_grant(tmp_path,method,monkeypatch):
    p=real_provider(ledger(tmp_path/'direct.sqlite3','REAL_LLM'),None,monkeypatch)
    with pytest.raises(ProviderError):
        if method=='prepare':p.prepare()
        elif method=='secret':p.secret()
        elif method=='complete':asyncio.run(p.complete(rid(),'planner',time.monotonic()+1))
        else:asyncio.run(p.exchange(rid(),'planner',time.monotonic()+1,{},''))

@pytest.mark.parametrize('case',['401','403','429','500','302','blank','invalid_json','length','large','missing_usage','bad_usage','injection','tool','slow','seed','models','unknown_model'])
def test_r3_loopback_failures_no_retry_or_refund(local,client,tmp_path,case,monkeypatch):
    old,_,state=local;s,t=session();l=ledger(tmp_path/'failure.sqlite3');p=G4R3SimulatedProvider(l,client.app.state,old.endpoint)
    state['proposal']=plan(44);state['case']=case if case not in {'seed','models','unknown_model'} else 'success'
    if case=='seed':state['proposal']['seed']=42
    if case=='models':state['proposal']['models']=['dummy'];state['proposal']['tools']=PLANNER_EXAMPLE['tools']
    if case=='unknown_model':
        original=httpx.AsyncClient.send
        async def unknown(self,request,*a,**kw):
            response=await original(self,request,*a,**kw);await response.aread()
            data=response.json();data['model']='TEST_ONLY_UNKNOWN_MODEL'
            return httpx.Response(200,json=data,request=request)
        monkeypatch.setattr(httpx.AsyncClient,'send',unknown)
    r=rid();l.reserve('main:44',r)
    async def call():
        with s.action('start') as grant:
            grant.bind(r)
            return await p.complete(r,'planner',time.monotonic()+(.04 if case=='slow' else 5))
    with pytest.raises(ProviderError):asyncio.run(call())
    before=l.snapshot()
    assert before['sent_or_unknown']==1 and before['tokens_reserved']==7978 and not state['authorization_seen']
    assert state['calls'] in {0,1} if case=='slow' else state['calls']==1
    with pytest.raises(ProviderError):asyncio.run(call())
    assert l.snapshot()==before and state['calls']<=1

@pytest.mark.parametrize('spoof',['sim_to_official','real_mode_label'])
def test_r3_endpoint_and_mode_spoof_rejected(client,tmp_path,spoof,monkeypatch):
    l=ledger(tmp_path/'spoof.sqlite3')
    if spoof=='sim_to_official':p=G4R3SimulatedProvider(l,client.app.state,'http://127.0.0.1:19999');p.endpoint=policy.ENDPOINT
    else:p=real_provider(ledger(tmp_path/'real-spoof.sqlite3','REAL_LLM'),client.app.state,monkeypatch);p.mode='SIMULATED'
    with pytest.raises(ProviderError):p.transport_guard()

def test_r3_genuine_hard_gate_refusal_not_fake_verified(client):
    from app.g4r3_science import science_summary
    u=RepeatUnit(seed=44,intent='balanced_comparison')
    parent=rid();child='run_'+'a'*32
    client.app.state.orchestration.create_run({'run_id':parent,'state':'RUNNING','nodes':{},'unit_key':'main:44','seed':44,'science_run_id':child})
    client.app.state.orchestration.create_run({'run_id':child,'state':'SUCCEEDED','verification_status':'VERIFIED','nodes':{str(i):{'state':'SUCCEEDED'} for i in range(9)}})
    with pytest.raises(ProviderError,match='FAKE_VERIFIED'):science_summary(client.app.state,parent,child,u)

def test_r3_real_cancellation_sibling_protected(client,monkeypatch):
    s,t=session();client.app.state.g4r3_approval=s;child='run_'+'b'*32;s.protected_runs.add(child);calls=[]
    monkeypatch.setattr(client.app.state.controller,'cancel',lambda r:calls.append(r) or {})
    assert client.post('/api/runs/'+child+'/cancel',headers=headers()).status_code==401
    assert calls==[]
    assert client.post('/api/runs/'+child+'/cancel',headers=headers(t)).status_code==200

@pytest.mark.parametrize('review_case',['accept','forged','recommend'])
def test_r3_new_seed44_science_end_to_end_simulated(local,client,tmp_path,monkeypatch,review_case):
    old,_,state=local;s,t=session();client.app.state.g4r3_approval=s
    l=ledger(tmp_path/'science.sqlite3');p=G4R3SimulatedProvider(l,client.app.state,old.endpoint);service=G4R3Service(client.app.state,l,p)
    client.app.state.g4r3_service=service;state['proposal']={**plan(44),'strategy':'dynamic','verification':'verify_and_repair'}
    original=p.complete
    async def complete(run_id,role,deadline):
        if role=='reviewer':
            _,_,summary,_=p.body(run_id,role)
            state['proposal']={'decision':'accept_evidence','evidence_ids':[summary['evidence_ids'][0]],'reason':'metrics_consistent'}
            if review_case=='forged':state['proposal']['evidence_ids']=['run_'+'b'*32+':1:'+'b'*64]
            if review_case=='recommend':state['proposal']['decision']='recommend_same_tool_reexecution'
        return await original(run_id,role,deadline)
    monkeypatch.setattr(p,'complete',complete)
    response=client.post('/api/g4r3/main',headers=headers(t),json={'seed':44})
    assert response.status_code==200
    result=response.json();assert result['run']['state']==('SUCCEEDED' if review_case=='accept' else 'FAILED'),result['run'].get('error')
    assert result['mode']=='SIMULATED' and result['run']['verification_status']==('VERIFIED' if review_case=='accept' else 'REJECTED') and result['science']['integrity']['ok']
    assert len(result['science']['run']['nodes'])==9 and state['calls']==2
    receipts=[r['receipt'] for r in result['role_receipts'] if r['kind']=='G4R3RoleResponse']
    assert len(receipts)==(2 if review_case=='accept' else 1) and all(r['mode']=='SIMULATED' and r['test_label']=='TEST_ONLY' and not r['official_response_received'] for r in receipts)
    assert result['run']['science_run_id'] in s.protected_runs and t not in json.dumps(result)
    assert client.post('/api/g4r3/main',headers=headers(t),json={'seed':44}).status_code==409
    assert state['calls']==2

def test_r3_actual_science_hard_gate_rejects_and_stops_before_review(local,client,tmp_path,monkeypatch):
    from app.g4r3_service import compile_proposal as original
    old,_,state=local;s,t=session();client.app.state.g4r3_approval=s;l=ledger(tmp_path/'reject.sqlite3')
    p=G4R3SimulatedProvider(l,client.app.state,old.endpoint);client.app.state.g4r3_service=G4R3Service(client.app.state,l,p);state['proposal']=plan(44)
    def injected(plan,controller):
        spec=original(plan,controller)
        binding=next(n for n in spec.nodes if n.id=='leakage').inputs['features']
        binding.value=[*binding.value,'TWF']  # TEST_ONLY copy, never mutate the global FEATURES list.
        return spec
    monkeypatch.setattr('app.g4r3_service.compile_proposal',injected)
    response=client.post('/api/g4r3/main',headers=headers(t),json={'seed':44})
    assert response.status_code==200
    value=response.json();assert value['run']['state']=='FAILED' and value['science']['run']['verification_status']=='REJECTED'
    assert state['calls']==1 and value['mode']=='SIMULATED' and value['test_label']=='TEST_ONLY'
    assert not any(a['tool']=='model_trainer' for a in value['science']['artifacts'])
    assert l.snapshot()['stop_reason'] and l.snapshot()['tokens_reserved']==7978

def test_r3_connection_failure_is_unknown_billing_no_retry(client,tmp_path):
    import socket
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    l=ledger(tmp_path/'connect.sqlite3');p=G4R3SimulatedProvider(l,client.app.state,f'http://127.0.0.1:{port}');s,t=session();r=rid();l.reserve('main:44',r)
    async def run():
        with s.action('start') as g:g.bind(r);return await p.complete(r,'planner',time.monotonic()+5)
    with pytest.raises(ProviderError):asyncio.run(run())
    assert l.snapshot()['sent_or_unknown']==1 and l.snapshot()['tokens_reserved']==7978
    with pytest.raises(ProviderError):asyncio.run(run())

def test_r3_missing_key_after_review_gate_still_no_reservation(client,tmp_path,monkeypatch):
    s,t=session();client.app.state.g4r3_approval=s;l=ledger(tmp_path/'nokey.sqlite3','REAL_LLM');p=real_provider(l,client.app.state,monkeypatch)
    monkeypatch.setattr('app.g4r3_provider.phase_gate',lambda:None)
    monkeypatch.delenv('VERIFLOW_G4R3_API_KEY',raising=False)
    client.app.state.g4r3_service=G4R3Service(client.app.state,l,p)
    r=client.post('/api/g4r3/main',headers=headers(t),json={'seed':44})
    assert r.status_code==409 and r.json()['error']['code']=='KEY_NOT_CONFIGURED'
    assert l.snapshot()['requests_reserved']==0 and client.app.state.orchestration.runs()==[]

def test_r3_concurrent_and_cancel_stops_batch(local,client,tmp_path,monkeypatch):
    import threading
    old,_,state=local;s,t=session();client.app.state.g4r3_approval=s;l=ledger(tmp_path/'cancel.sqlite3')
    p=G4R3SimulatedProvider(l,client.app.state,old.endpoint);service=G4R3Service(client.app.state,l,p);client.app.state.g4r3_service=service
    entered=threading.Event()
    async def pending(*args):
        entered.set();await asyncio.sleep(30)
    monkeypatch.setattr(p,'complete',pending)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(client.post,'/api/g4r3/main',headers=headers(t),json={'seed':44})
        assert entered.wait(5)
        r=next(iter(service.tasks));before=l.snapshot()
        assert client.post('/api/g4r3/main',headers=headers(t),json={'seed':45}).status_code==409
        assert client.post('/api/g4r3/'+r+'/cancel',headers=headers()).status_code==401
        assert before==l.snapshot()
        assert client.post('/api/g4r3/'+r+'/cancel',headers=headers(t)).status_code==200
        result=first.result(timeout=10).json()
    assert result['run']['state']=='FAILED' and l.snapshot()['stop_reason']
    assert l.snapshot()['tokens_reserved']==7978 and state['calls']==0
    assert client.post('/api/g4r3/main',headers=headers(t),json={'seed':44}).status_code==409

def test_r3_optional_planner_only_no_science_or_reviewer(local,client,tmp_path):
    old,_,state=local;s,t=session();client.app.state.g4r3_approval=s;l=ledger(tmp_path/'extra.sqlite3')
    for seed in range(44,47):completed(l,f'main:{seed}')  # TEST_ONLY setup, not real inference.
    p=G4R3SimulatedProvider(l,client.app.state,old.endpoint);client.app.state.g4r3_service=G4R3Service(client.app.state,l,p)
    state['proposal']={**plan(42),'reason':'recall_quality'}
    response=client.post('/api/g4r3/extra',headers=headers(t),json={'intent':'recall_quality'})
    assert response.status_code==200
    r=response.json();assert r['run']['state']=='SUCCEEDED' and r['run']['verification_status']=='PLANNER_ONLY_NOT_SCIENCE_VERIFIED'
    assert set(r['run']['nodes'])=={'planner'} and 'science' not in r and state['calls']==1
    assert client.post('/api/g4r3/extra',headers=headers(t),json={'intent':'recall_quality'}).status_code==409

def test_r3_review_context_cannot_be_supplied_by_caller(client,tmp_path):
    l=ledger(tmp_path/'review.sqlite3');p=G4R3SimulatedProvider(l,client.app.state,'http://127.0.0.1:19999');r=rid();l.reserve('main:44',r)
    with pytest.raises(ProviderError,match='NO_TRUSTED_REVIEW'):p.body(r,'reviewer')

def test_r3_expiry_between_roles_preserves_preallocation(local,client,tmp_path):
    old,_,state=local;s,t=session();now=[policy.START+60];l=G4R3Ledger(tmp_path/'expiry.sqlite3','SIMULATED',clock=lambda:now[0])
    p=G4R3SimulatedProvider(l,client.app.state,old.endpoint);state['proposal']=plan(44);r=rid();l.reserve('main:44',r)
    async def run():
        with s.action('start') as g:
            g.bind(r);await p.complete(r,'planner',time.monotonic()+5)
            now[0]=policy.EXPIRES
            with pytest.raises(ProviderError,match='AUTHORIZATION_EXPIRED'):await p.complete(r,'reviewer',time.monotonic()+5)
    asyncio.run(run());assert state['calls']==1 and l.snapshot()['tokens_reserved']==7978

def test_r3_ready_and_release_are_bound_to_actual_hashes(tmp_path,monkeypatch):
    import os
    monkeypatch.setattr(policy,'time',SimpleNamespace(time=lambda:policy.START+60))
    original_auth=policy.authorization
    monkeypatch.setattr(policy,'authorization',lambda now=None:original_auth(policy.START+60 if now is None else now))
    monkeypatch.setattr(policy,'OUT',tmp_path)
    source=policy.ROOT/'backend/app/g4r3_policy.py'
    data={'tests_passed':True,'authorization_sha256':policy.SHA,'source_sha256':{'backend/app/g4r3_policy.py':hashlib.sha256(source.read_bytes()).hexdigest()},'budget':policy.dryrun()}
    ready=tmp_path/'READY.json';ready.write_text(json.dumps(data),encoding='utf-8')
    ready_sha=policy.verify_ready()
    with pytest.raises(ProviderError,match='AWAITING_CHATGPT_G4R3_PREKEY_REVIEW'):policy.phase_gate(local_switch=False)
    review=tmp_path/'TEST_ONLY_REVIEW.md';review.write_text('TEST_ONLY not an actual Gate review',encoding='utf-8')
    release=tmp_path/'CHATGPT_PREKEY_RELEASE.json'
    value={'status':'PASS','authorization_id':policy.ID,'ready_sha256':ready_sha,
        'review_path':review.relative_to(policy.ROOT).as_posix(),'review_sha256':hashlib.sha256(review.read_bytes()).hexdigest(),
        'pricing_source':'https://api-docs.deepseek.com/quick_start/pricing/','input_rate':.30,'output_rate':1.20,
        'price_checked_at':'2026-11-08T13:42:17+08:00'}
    release.write_text(json.dumps(value),encoding='utf-8');policy.phase_gate(local_switch=False)
    monkeypatch.delenv('G4R3_BACKEND_STARTED',raising=False)
    with pytest.raises(ProviderError,match='LOCAL_START_REQUIRED'):policy.phase_gate()
    value['ready_sha256']='0'*64;release.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ProviderError):policy.phase_gate(local_switch=False)
    data['source_sha256']['backend/app/g4r3_policy.py']='0'*64;ready.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ProviderError,match='SOURCE_CHANGED'):policy.verify_ready()

def test_r3_cors_and_no_origin_cli_with_explicit_approval(client):
    s,t=session();client.app.state.g4r3_approval=s
    h={'Origin':'http://127.0.0.1:15173','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':HEADER}
    response=client.options('/api/g4r3/main',headers=h)
    assert response.status_code==200 and HEADER in response.headers['access-control-allow-headers'].lower()
    h['Origin']='https://evil.example';assert client.options('/api/g4r3/main',headers=h).status_code==400
    # Correct approval without Origin passes authentication, then safely stops at Phase P service gate.
    r=client.post('/api/g4r3/main',headers=headers(t),json={'seed':44})
    assert r.status_code==403 and r.json()['error']['code']=='AWAITING_CHATGPT_G4R3_PREKEY_REVIEW'

@pytest.mark.parametrize('body',[{'seed':44.0},{'seed':'43'},{'seed':41},{'seed':47},{'seed':44,'intent':'latency'},{'seed':44,'api_key':'TEST_ONLY'}])
def test_r3_main_body_cannot_override_matrix(client,body):
    s,t=session();client.app.state.g4r3_approval=s
    r=client.post('/api/g4r3/main',headers=headers(t),json=body)
    assert r.status_code==422 and client.app.state.orchestration.runs()==[]

def test_r3_ready_rejects_changed_body_within_token_caps(tmp_path,monkeypatch):
    monkeypatch.setattr(policy,'OUT',tmp_path)
    source=policy.ROOT/'backend/app/g4r3_policy.py'
    data={'tests_passed':True,'authorization_sha256':policy.SHA,'source_sha256':{'backend/app/g4r3_policy.py':hashlib.sha256(source.read_bytes()).hexdigest()},'budget':policy.dryrun()}
    (tmp_path/'READY.json').write_text(json.dumps(data),encoding='utf-8')
    original=policy.repeat_body
    def changed(*args,**kwargs):
        b=original(*args,**kwargs);b['messages'][0]['content']+=' TEST_ONLY';return b
    monkeypatch.setattr(policy,'repeat_body',changed)
    assert policy.dryrun()['totals']['tokens']<32000
    with pytest.raises(ProviderError,match='BODY_OR_BUDGET_CHANGED'):policy.verify_ready()

def test_r3_real_provider_cannot_reset_budget_with_another_real_ledger(tmp_path):
    l=ledger(tmp_path/'unrelated-real.sqlite3','REAL_LLM')
    with pytest.raises(ProviderError,match='PRODUCTION_LEDGER_REQUIRED'):G4R3Provider(l,None)
    assert l.snapshot()['requests_reserved']==0

@pytest.fixture(autouse=True)
def capture_r3_failure_ledger(request,tmp_path):
    """Persist actual loopback fixture DB receipts, never a fabricated failure."""
    yield
    if request.node.originalname!='test_r3_loopback_failures_no_retry_or_refund':return
    path=tmp_path/'failure.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        tables={t:[dict(zip([c[1] for c in db.execute('PRAGMA table_info("'+t+'")')],r)) for r in db.execute('SELECT * FROM "'+t+'"')] for t in ('policy','units','slots','journal','control')}
    directory=policy.OUT/'negative-ledgers';directory.mkdir(parents=True,exist_ok=True)
    data={'mode':'SIMULATED','test_label':'TEST_ONLY','case':request.node.callspec.params['case'],
        'test':request.node.nodeid,'source_db':path.relative_to(policy.ROOT).as_posix(),
        'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'tables':tables,'new_paid_requests':0}
    with (directory/(uuid4().hex+'.json')).open('x',encoding='utf-8') as f:json.dump(data,f,indent=2)

@pytest.mark.parametrize('seed',[42,43,41,47])
def test_r3_forbidden_primary_seeds_no_reserve_no_provider(client,tmp_path,seed):
    s,t=session();client.app.state.g4r3_approval=s;l=ledger(tmp_path/'forbidden.sqlite3')
    before=l.snapshot()
    with pytest.raises(ProviderError):l.reserve('main:'+str(seed),rid())
    assert client.post('/api/g4r3/main',headers=headers(t),json={'seed':seed}).status_code==422
    assert l.snapshot()==before and client.app.state.orchestration.runs()==[]

@pytest.mark.parametrize('route',['/api/g4r3/main','/api/g4r3/extra','/api/g4r3/run_g4r3_TEST_ONLY/cancel'])
@pytest.mark.parametrize('case',['old_header','old_type','old_token'])
def test_r3_old_r2_approval_cannot_authorize_r3(client,route,case):
    from app.g4r2_approval import R2ApprovalSession,HEADER as OLD_HEADER
    from app.g4r2_policy import START as OLD_START
    old,token=R2ApprovalSession.issue(wall=lambda:OLD_START+60)
    new,new_token=session();client.app.state.g4r3_approval=new
    h=headers(new_token)
    if case=='old_header':h.pop(HEADER);h[OLD_HEADER]=token
    if case=='old_type':client.app.state.g4r3_approval=old;h=headers(token)
    if case=='old_token':h=headers(token)
    r=client.post(route,headers=h,content=b'NOT_JSON_TEST_ONLY')
    assert r.status_code==401 and client.app.state.orchestration.runs()==[]
    assert token not in r.text and new_token not in r.text

@pytest.mark.parametrize('seed',[45,46])
def test_r3_sequential_execution_and_independent_main44_review(tmp_path,seed):
    l=ledger(tmp_path/'sequential.sqlite3','REAL_LLM')
    with pytest.raises(ProviderError,match='SEQUENTIAL_MAIN_REQUIRED'):l.reserve(f'main:{seed}',rid())
    completed(l,'main:44')  # TEST_ONLY raw fixture settlement, not an official response.
    before=l.snapshot()
    with pytest.raises(ProviderError,match='AWAITING_CHATGPT_G4R3_MAIN44_REVIEW'):l.reserve(f'main:{seed}',rid())
    assert l.snapshot()==before

def test_r3_simulation_must_still_complete_45_before_46(tmp_path):
    l=ledger(tmp_path/'sim-sequence.sqlite3');completed(l,'main:44')
    with pytest.raises(ProviderError,match='SEQUENTIAL_MAIN_REQUIRED'):l.reserve('main:46',rid())
    completed(l,'main:45');completed(l,'main:46')
    assert l.snapshot()['requests_reserved']==6

def test_r3_main44_release_is_run_and_hash_bound(tmp_path,monkeypatch):
    monkeypatch.setattr(policy,'OUT',tmp_path);r=rid()
    with pytest.raises(ProviderError,match='MAIN44_REVIEW'):policy.progression_review(r)
    directory=tmp_path/'real-runs';directory.mkdir()
    record={'mode':'REAL_LLM','run':{'unit_key':'main:44','run_id':r,'state':'SUCCEEDED','verification_status':'VERIFIED'}}
    path=directory/(r+'.json');path.write_text(json.dumps(record))
    review=tmp_path/'TEST_ONLY_NOT_AN_ACTUAL_REVIEW.md';review.write_text('TEST_ONLY fixture')
    release={'status':'PASS','authorization_id':policy.ID,'run_id':r,'run_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'review_path':review.relative_to(policy.ROOT).as_posix(),'review_sha256':hashlib.sha256(review.read_bytes()).hexdigest()}
    p=tmp_path/'CHATGPT_MAIN44_RELEASE.json';p.write_text(json.dumps(release));policy.progression_review(r)
    with pytest.raises(ProviderError):policy.progression_review(rid())
    review.write_text('TEST_ONLY changed')
    with pytest.raises(ProviderError):policy.progression_review(r)
    release['review_sha256']=hashlib.sha256(review.read_bytes()).hexdigest();p.write_text(json.dumps(release))
    record['mode']='SIMULATED';path.write_text(json.dumps(record));release['run_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();p.write_text(json.dumps(release))
    with pytest.raises(ProviderError):policy.progression_review(r)

@pytest.mark.parametrize('key',['main:42','main:43','main:044','extra:balanced_comparison'])
def test_r3_only_registered_fixed_units(key):
    with pytest.raises(ProviderError):policy.unit(key)

@pytest.mark.parametrize('url',['http://api.deepseek.com/chat/completions','https://evil.example/chat/completions',
    'https://api.deepseek.com.evil.example/chat/completions','https://api.deepseek.com/chat/completions?x=1',
    'https://api.deepseek.com/chat/completions/','https://api.deepseek.com:444/chat/completions'])
def test_r3_official_provider_exact_endpoint(client,tmp_path,monkeypatch,url):
    l=ledger(tmp_path/'url.sqlite3','REAL_LLM');p=real_provider(l,client.app.state,monkeypatch);p.endpoint=url
    with pytest.raises(ProviderError,match='ENDPOINT_REJECTED'):p.transport_guard()
    assert l.snapshot()['sent_or_unknown']==0

@pytest.mark.parametrize('url',['https://127.0.0.1:12345','http://localhost:12345','http://127.0.0.1:12345/proxy',
    'http://127.0.0.1:12345?url=x','http://user@127.0.0.1:12345'])
def test_r3_simulated_transport_has_no_proxy_path(client,tmp_path,url):
    with pytest.raises(ProviderError):G4R3SimulatedProvider(ledger(tmp_path/'url.sqlite3'),client.app.state,url)

def test_r3_history_is_genuine_but_not_new_credit():
    from app.g4r3_history import verify
    result=verify()
    assert [x['seed'] for x in result]==[42,43] and all(x['new_budget_requests']==0 for x in result)
    assert result[0]['verification']=='verify_and_repair' and result[1]['verification']=='verify_only'

@pytest.mark.parametrize('case',['binding','child','mode','official','prompt','evidence','chain'])
def test_r3_history_rejects_tampered_binding_or_evidence(case):
    from app.g4r3_history import validate_record
    path=policy.ROOT/'reports/G4/deepseek/real/g4r2/real-runs/run_g4r2_86e89f4b899a43ad9c7317038a49d4c9.json'
    d=json.loads(path.read_text(encoding='utf-8'))
    if case=='binding':d['run']['seed']=43
    if case=='child':d['run']['science_run_id']='run_'+'f'*32
    if case=='mode':d['mode']='SIMULATED'
    if case=='chain':d['science']['verification'][0]['receipt_hash']='0'*64
    if case in {'official','prompt','evidence'}:
        roles=[r['receipt'] for r in d['role_receipts'] if r['kind']=='G4R2RoleResponse']
        if case=='official':roles[0]['official_response_received']=False
        if case=='prompt':roles[0]['prompt_version']='real-planner-v1'
        if case=='evidence':roles[1]['proposal']['evidence_ids']=['run_'+'f'*32+':1:'+'f'*64]
        previous='0'*64
        from app.workflow_models import digest
        for r in d['role_receipts']:
            r['previous_hash']=previous
            payload={k:v for k,v in r.items() if k not in {'kind','previous_hash','receipt_hash','evidence_id'}}
            r['receipt_hash']=digest({'kind':r['kind'],'payload':payload,'previous_hash':previous})
            r['evidence_id']=f"{r['run_id']}:{r['seq']}:{r['receipt_hash']}";previous=r['receipt_hash']
    with pytest.raises(ProviderError):validate_record(d,42)

def test_r3_cohort_package_drift_requires_stop(monkeypatch):
    from app import g4r3_history as history
    monkeypatch.setattr(history,'version',lambda name:'TEST_ONLY_DIFFERENT_ENVIRONMENT')
    with pytest.raises(ProviderError,match='COHORT_ENVIRONMENT_CHANGED'):history.verify()

def test_r3_old_production_authorizations_remain_expired():
    from app.deepseek_authorization import authorization as first
    from app.g4r2_policy import authorization as second
    for old in (first,second):
        with pytest.raises(ProviderError,match='EXPIRED'):old(policy.START+60)

def test_r3_foreign_ledger_rejected_before_write(tmp_path):
    source=policy.ROOT/'runtime/deepseek_g4r2.sqlite3';p=tmp_path/'old-copy.sqlite3';p.write_bytes(source.read_bytes())
    before=p.read_bytes()
    with pytest.raises(ProviderError,match='NAMESPACE_MISMATCH'):ledger(p)
    assert p.read_bytes()==before

@pytest.fixture
def isolated_r3_status_ledger(tmp_path,monkeypatch):
    """Inject only this test's ledger root; keep the actual SQLite reader intact.

    No HTTP/environment selector is added. Production ROOT/path and its ledger
    remain untouched. pytest restores this module attribute after the test.
    """
    from app import g4r3_api
    root=tmp_path/'status-ledger-root'
    path=root/'runtime/deepseek_g4r3.sqlite3'
    G4R3Ledger(path,mode='REAL_LLM')  # Real policy/control schema, no request.
    monkeypatch.setattr(g4r3_api,'ROOT',root)
    return path

def test_r3_phase_p_get_never_reports_new_history_as_r3_credit(client,isolated_r3_status_ledger):
    path=isolated_r3_status_ledger;before=path.read_bytes()
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('SELECT COUNT(*) FROM policy').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM control').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM slots').fetchone()[0]==0
    response=client.get('/api/g4r3/status');assert response.status_code==200
    value=response.json();assert [x['seed'] for x in value['historical_main']]==[42,43]
    assert value['key_read'] is False and value['mode']=='PHASE_P_NO_KEY'
    assert value['budget'] is None or value['budget']['requests_reserved']==0
    assert value['available_units']==['main:44']
    assert value['budget'] is not None and value['budget']['requests_reserved']==0
    assert path.read_bytes()==before
    _f1_status_proof('empty',path,value)

def _f1_status_proof(scenario,path,value):
    """New F1 evidence only. No credentials or production writes."""
    import hashlib
    directory=policy.ROOT/'reports/G4/g4-fix-20261109-f1-resume/proofs'
    directory.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        counts={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('policy','control','units','slots','journal')}
    proof={'scenario':scenario,'test_label':'TEST_ONLY','path':path.relative_to(policy.ROOT).as_posix(),
        'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'table_counts':counts,'response':value,'get_changed_bytes':False,'new_paid_requests':0}
    with (directory/(scenario+'-'+uuid4().hex+'.json')).open('x',encoding='utf-8') as f:json.dump(proof,f,indent=2)

def test_f1_exhausted_isolated_ledger_is_readonly(client,isolated_r3_status_ledger):
    path=isolated_r3_status_ledger
    # TEST_ONLY synthetic completed observations, not new REAL_LLM calls. Use
    # all registered units/actual bounds and the immutable REAL policy schema.
    with sqlite3.connect(path) as db:
        for row in policy.dryrun()['units']:
            run=rid();db.execute('INSERT INTO units VALUES(?,?,?,NULL)',(row['unit_key'],run,'SUCCEEDED'))
            for role,bound in row['input_bounds'].items():
                from app.deepseek_repeat_prep import micro_usd
                payload=json.dumps({'mode':'SIMULATED','test_label':'TEST_ONLY','official_response_received':False})
                db.execute('INSERT INTO slots(run_id,role,input_bound,tokens,microusd,status,request_hash,payload) VALUES(?,?,?,?,?,?,?,?)',
                    (run,role,bound,bound+800,micro_usd(bound),'COMPLETED','TEST_ONLY',payload))
    before=path.read_bytes()
    for _ in range(2):
        response=client.get('/api/g4r3/status');assert response.status_code==200
        value=response.json();budget=value['budget']
        assert budget['requests_reserved']==8 and budget['requests_remaining']==0 and budget['sent_or_unknown']==8
        assert budget['tokens_reserved']==30954 and budget['tokens_remaining']==1046
        assert budget['usd_reserved']==pytest.approx(.015049) and budget['usd_remaining']==pytest.approx(.014951)
        assert [h['seed'] for h in value['historical_main']]==[42,43]
        assert {u['unit_key'] for u in budget['units']}=={'main:44','main:45','main:46','extra:recall_quality','extra:latency'}
        assert value['available_units']==[] and value['key_read'] is False and value['mode']=='PHASE_P_NO_KEY'
        assert client.app.state.orchestration.runs()==[] and path.read_bytes()==before
    _f1_status_proof('exhausted',path,value)

@pytest.mark.parametrize('selector',['query','header','get_body'])
def test_f1_http_cannot_select_an_empty_ledger(client,tmp_path,selector):
    from app import g4r3_api
    assert g4r3_api.ROOT==policy.ROOT  # Previous scoped override was restored.
    path=tmp_path/'untrusted-empty.sqlite3';G4R3Ledger(path,mode='REAL_LLM')
    prod=policy.ROOT/'runtime/deepseek_g4r3.sqlite3';before=prod.read_bytes()
    kwargs={}
    if selector=='query':kwargs['params']={'ledger_path':str(path),'ROOT':str(tmp_path)}
    if selector=='header':kwargs['headers']={'X-Ledger-Path':str(path)}
    if selector=='get_body':kwargs['json']={'ledger_path':str(path)}
    response=client.request('GET','/api/g4r3/status',**kwargs);assert response.status_code==200
    value=response.json();assert value['budget']['requests_reserved']==8 and value['budget']['requests_remaining']==0
    assert value['key_read'] is False and client.app.state.orchestration.runs()==[] and prod.read_bytes()==before
    _f1_status_proof('production-selector-'+selector,prod,value)

@pytest.mark.parametrize('bad',['missing','rate','source','future','naive'])
def test_r3_price_release_must_match_official_conservative_rates(tmp_path,monkeypatch,bad):
    monkeypatch.setattr(policy,'time',SimpleNamespace(time=lambda:policy.START+60))
    original_auth=policy.authorization
    monkeypatch.setattr(policy,'authorization',lambda now=None:original_auth(policy.START+60 if now is None else now))
    monkeypatch.setattr(policy,'OUT',tmp_path)
    source=policy.ROOT/'backend/app/g4r3_policy.py'
    data={'tests_passed':True,'authorization_sha256':policy.SHA,'source_sha256':{'backend/app/g4r3_policy.py':hashlib.sha256(source.read_bytes()).hexdigest()},'budget':policy.dryrun()}
    (tmp_path/'READY.json').write_text(json.dumps(data))
    review=tmp_path/'TEST_ONLY_PRICE_REVIEW.md';review.write_text('TEST_ONLY fixture, no real release')
    value={'status':'PASS','authorization_id':policy.ID,'ready_sha256':policy.verify_ready(),
        'review_path':review.relative_to(policy.ROOT).as_posix(),'review_sha256':hashlib.sha256(review.read_bytes()).hexdigest(),
        'pricing_source':'https://api-docs.deepseek.com/quick_start/pricing/','input_rate':.30,'output_rate':1.20,
        'price_checked_at':'2026-11-08T13:42:17+08:00'}
    if bad=='missing':value.pop('price_checked_at')
    if bad=='rate':value['input_rate']=.15
    if bad=='source':value['pricing_source']='https://evil.example/'
    if bad=='future':value['price_checked_at']='2026-11-10T13:42:17+08:00'
    if bad=='naive':value['price_checked_at']='2026-11-08T13:42:17'
    (tmp_path/'CHATGPT_PREKEY_RELEASE.json').write_text(json.dumps(value))
    with pytest.raises(ProviderError,match='PRICE_REVIEW_REQUIRED'):policy.phase_gate(local_switch=False)

def test_r3_history_manifest_hash_is_immutable(tmp_path,monkeypatch):
    from app import g4r3_history as history
    p=tmp_path/'TEST_ONLY_PINS.json';p.write_text('{}');monkeypatch.setattr(history,'PINS',p)
    with pytest.raises(ProviderError,match='PINS_CHANGED'):history.verify()

def test_r3_historical_file_pin_is_enforced(tmp_path,monkeypatch):
    from app import g4r3_history as history
    value=json.loads(history.PINS.read_text(encoding='utf-8'))
    value['files']['backend/app/science_contracts.py']='0'*64
    p=tmp_path/'TEST_ONLY_PINS.json';p.write_text(json.dumps(value))
    monkeypatch.setattr(history,'PINS',p);monkeypatch.setattr(history,'PINS_SHA',hashlib.sha256(p.read_bytes()).hexdigest())
    with pytest.raises(ProviderError,match='HISTORY_OR_COHORT_CHANGED'):history.verify()

def test_r3_historical_fee_rows_are_readonly_and_checked(tmp_path,monkeypatch):
    from app import g4r3_history as history
    path=tmp_path/'TEST_ONLY_OLD_LEDGER.sqlite3';path.write_bytes((policy.ROOT/'runtime/deepseek_g4r2.sqlite3').read_bytes())
    connect=sqlite3.connect
    with connect(path) as db:db.execute("UPDATE units SET state='FAILED' WHERE unit_key='main:42'")
    def replaced(name,*args,**kw):
        if 'deepseek_g4r2.sqlite3' in str(name):return connect(path.as_uri()+'?mode=ro',uri=True)
        return connect(name,*args,**kw)
    monkeypatch.setattr(history.sqlite3,'connect',replaced)
    with pytest.raises(ProviderError,match='HISTORICAL_DATABASE_CHANGED'):history.verify()
