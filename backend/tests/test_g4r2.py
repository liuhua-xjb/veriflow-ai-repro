"""G4R2 Phase P. Synthetic tokens, loopback HTTP and isolated DBs only."""
import asyncio,copy,json,sqlite3,time,hashlib
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from uuid import uuid4
import httpx,pytest
from test_deepseek import local
from app import g4r2_policy as policy
from app.g4r2_ledger import G4R2Ledger
from app.g4r2_approval import R2ApprovalSession,HEADER
from app.g4r2_provider import G4R2Provider,G4R2SimulatedProvider
from app.g4r2_service import G4R2Service
from app.deepseek_repeat_prep import RepeatUnit,repeat_body
from app.deepseek_contracts import PLANNER_EXAMPLE
from app.llm import ProviderError

def rid():return 'run_g4r2_'+uuid4().hex
def ledger(path,mode='SIMULATED'):return G4R2Ledger(path,mode,clock=lambda:policy.START+60)
def session():return R2ApprovalSession.issue(wall=lambda:policy.START+60)
def real_provider(l,state,monkeypatch):
    # TEST_ONLY module constant replacement; no environment or API bypass switch.
    monkeypatch.setattr('app.g4r2_provider.PRODUCTION_LEDGER',l.path)
    return G4R2Provider(l,state)
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
def test_r2_absolute_authorization_boundaries(now):
    with pytest.raises(ProviderError):policy.authorization(now)
def test_r2_start_inclusive_and_receipt_sha(tmp_path,monkeypatch):
    assert policy.authorization(policy.START)['authorization_id']==policy.ID
    p=tmp_path/'receipt.md';p.write_text('TEST_ONLY forged receipt',encoding='utf-8')
    monkeypatch.setattr(policy,'RECEIPT',p)
    with pytest.raises(ProviderError,match='RECEIPT_MISMATCH'):policy.authorization(policy.START)
def test_r2_wrong_matrix_rejected(tmp_path,monkeypatch):
    p=tmp_path/'matrix.md';p.write_text('TEST_ONLY',encoding='utf-8');monkeypatch.setattr(policy,'MATRIX',p)
    with pytest.raises(ProviderError,match='MATRIX_MISMATCH'):policy.authorization(policy.START)

def test_r2_policy_old_ledger_and_mode_separation(tmp_path):
    old=policy.ROOT/'runtime/deepseek_real_trial.sqlite3';before=hashlib.sha256(old.read_bytes()).hexdigest()
    with pytest.raises(ProviderError):G4R2Ledger(old)
    l=ledger(tmp_path/'r2.sqlite3')
    with pytest.raises(ProviderError):ledger(l.path,'REAL_LLM')
    assert hashlib.sha256(old.read_bytes()).hexdigest()==before
    with l.db() as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE policy SET payload='fake'")

def test_r2_revised_budget_and_same_intent():
    d=policy.dryrun();main=d['units'][:5];extra=d['units'][5:]
    assert {r['intent'] for r in main}=={'balanced_comparison'} and [r['seed'] for r in main]==list(range(42,47))
    assert sum(r['requests'] for r in main)==10 and sum(r['tokens'] for r in main)==39890
    assert all(set(r['input_bounds'])=={'planner'} for r in extra)
    assert d['totals']=={'requests':12,'tokens':46910,'microusd':22717}
def test_r2_schema_growth_stops_before_spend(monkeypatch,tmp_path):
    monkeypatch.setattr(policy,'input_bound',lambda body:4001)
    with pytest.raises(ProviderError,match='INPUT_POLICY'):ledger(tmp_path/'x.sqlite3').reserve('main:43',rid())

def race_reserve(path):
    try:ledger(Path(path)).reserve('main:43',rid());return True
    except ProviderError:return False
def test_r2_real_multiprocess_reservation_race(tmp_path):
    import multiprocessing
    l=ledger(tmp_path/'race.sqlite3')
    with ProcessPoolExecutor(3,mp_context=multiprocessing.get_context('spawn')) as pool:
        assert sum(pool.map(race_reserve,[str(l.path)]*3))==1
    assert l.snapshot()['requests_reserved']==2

def test_r2_restart_no_credit_refresh_no_retry(tmp_path):
    path=tmp_path/'restart.sqlite3';l=ledger(path);r=rid();l.reserve('main:42',r);l.dispatch(r,'planner','h',2713)
    reopened=ledger(path)
    with pytest.raises(ProviderError):reopened.dispatch(r,'planner','h',2713)
    before=reopened.snapshot();reopened.recover();after=reopened.snapshot()
    assert before['tokens_reserved']==after['tokens_reserved']==7978 and after['stop_reason']=='PROCESS_INTERRUPTED'
    with pytest.raises(ProviderError,match='BATCH_STOPPED'):reopened.reserve('main:43',rid())

@pytest.mark.parametrize('kind',['tokens_exact','tokens_one_over','cost_exact','cost_one_over','requests'])
def test_r2_budget_last_token_cost_and_request_boundary(tmp_path,kind):
    l=ledger(tmp_path/'limit.sqlite3');b=policy.bounds('main:43');tokens=sum(v+800 for v in b.values())
    from app.deepseek_repeat_prep import micro_usd
    cost=sum(micro_usd(v) for v in b.values())
    with l.db() as db:
        for i in range(11 if kind=='requests' else 1):
            t=50000-tokens+(1 if kind=='tokens_one_over' else 0) if kind.startswith('tokens') else 0
            c=50000-cost+(1 if kind=='cost_one_over' else 0) if kind.startswith('cost') else 0
            db.execute('INSERT INTO slots(run_id,role,input_bound,tokens,microusd,status,payload) VALUES(?,?,?,?,?,?,?)',('TEST_ONLY_'+str(i),'planner',1,t,c,'FAILED','{}'))
    if kind.endswith('exact'):
        l.reserve('main:43',rid());s=l.snapshot();assert s['tokens_remaining']==0 if kind.startswith('tokens') else s['usd_remaining']==0
    else:
        before=l.snapshot()
        with pytest.raises(ProviderError,match='BUDGET_EXHAUSTED'):l.reserve('main:43',rid())
        assert before==l.snapshot()

def test_r2_main_once_extras_only_after_five_and_no_reviewer(tmp_path):
    l=ledger(tmp_path/'queues.sqlite3')
    with pytest.raises(ProviderError,match='MAIN_QUEUE_NOT_COMPLETE'):l.reserve('extra:latency',rid())
    for seed in range(42,47):completed(l,f'main:{seed}')
    completed(l,'extra:recall_quality');r=completed(l,'extra:latency')
    assert l.snapshot()['requests_reserved']==12 and l.snapshot()['tokens_reserved']==46910
    with pytest.raises(ProviderError):l.reserve('main:43',rid())
    with pytest.raises(ProviderError):l.slot(r,'reviewer')

@pytest.mark.parametrize('route',['/api/g4r2/main','/api/g4r2/extra','/api/g4r2/run_g4r2_test/cancel'])
@pytest.mark.parametrize('case',['missing','wrong','expired','old_process','wrong_host','wrong_origin','null_origin','duplicate_token','duplicate_host','duplicate_origin','query','token_in_body'])
def test_r2_http_auth_denials_before_any_service(client,route,case):
    s,token=session();client.app.state.g4r2_approval=s;h=headers(token);body={}
    class Stub:
        calls=0
        async def start(self,key):self.calls+=1;return {}
        def cancel(self,key):self.calls+=1;return {}
    stub=Stub();client.app.state.g4r2_service=stub
    if case=='missing':h.pop(HEADER)
    if case=='wrong':h[HEADER]='0'*64
    if case=='expired':s._deadline=0
    if case=='old_process':client.app.state.g4r2_approval=session()[0]
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

def test_r2_legacy_session_cannot_authorize_new_round(client):
    from app.deepseek_approval import ApprovalSession
    s,token=ApprovalSession.issue();client.app.state.g4r2_approval=s
    assert client.post('/api/g4r2/main',headers=headers(token),json={'seed':43}).status_code==401
def test_r2_token_cleanup_and_later_absolute_deadline():
    s,t=R2ApprovalSession.issue(wall=lambda:policy.EXPIRES-1)
    s.authenticate(t);s.close()
    with pytest.raises(ProviderError):s.authenticate(t)

def test_r2_prekey_gate_before_key_read_or_ledger(client,tmp_path,monkeypatch):
    import os
    s,t=session();client.app.state.g4r2_approval=s;l=ledger(tmp_path/'real.sqlite3','REAL_LLM');p=real_provider(l,client.app.state,monkeypatch)
    client.app.state.g4r2_service=G4R2Service(client.app.state,l,p)
    monkeypatch.setattr('app.g4r2_provider.phase_gate',lambda:(_ for _ in ()).throw(ProviderError('AWAITING_CHATGPT_PREKEY_REVIEW')))
    original=os._Environ.get
    def get(env,key,*a):
        assert key not in {'VERIFLOW_G4R2_API_KEY','VERIFLOW_LLM_API_KEY'},'PHASE_P_KEY_READ'
        return original(env,key,*a)
    monkeypatch.setattr(os._Environ,'get',get)
    response=client.post('/api/g4r2/main',headers=headers(t),json={'seed':43})
    assert response.status_code==403 and response.json()['error']['code']=='AWAITING_CHATGPT_PREKEY_REVIEW'
    assert l.snapshot()['requests_reserved']==0 and client.app.state.orchestration.runs()==[]

@pytest.mark.parametrize('method',['prepare','secret','complete','exchange'])
def test_r2_direct_provider_calls_require_r2_grant(tmp_path,method,monkeypatch):
    p=real_provider(ledger(tmp_path/'direct.sqlite3','REAL_LLM'),None,monkeypatch)
    with pytest.raises(ProviderError):
        if method=='prepare':p.prepare()
        elif method=='secret':p.secret()
        elif method=='complete':asyncio.run(p.complete(rid(),'planner',time.monotonic()+1))
        else:asyncio.run(p.exchange(rid(),'planner',time.monotonic()+1,{},''))

@pytest.mark.parametrize('case',['401','403','429','500','302','blank','invalid_json','length','large','missing_usage','bad_usage','injection','tool','slow','seed','models','unknown_model'])
def test_r2_loopback_failures_no_retry_or_refund(local,client,tmp_path,case,monkeypatch):
    old,_,state=local;s,t=session();l=ledger(tmp_path/'failure.sqlite3');p=G4R2SimulatedProvider(l,client.app.state,old.endpoint)
    state['proposal']=plan(43);state['case']=case if case not in {'seed','models','unknown_model'} else 'success'
    if case=='seed':state['proposal']['seed']=42
    if case=='models':state['proposal']['models']=['dummy'];state['proposal']['tools']=PLANNER_EXAMPLE['tools']
    if case=='unknown_model':
        original=httpx.AsyncClient.send
        async def unknown(self,request,*a,**kw):
            response=await original(self,request,*a,**kw);await response.aread()
            data=response.json();data['model']='TEST_ONLY_UNKNOWN_MODEL'
            return httpx.Response(200,json=data,request=request)
        monkeypatch.setattr(httpx.AsyncClient,'send',unknown)
    r=rid();l.reserve('main:43',r)
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
def test_r2_endpoint_and_mode_spoof_rejected(client,tmp_path,spoof,monkeypatch):
    l=ledger(tmp_path/'spoof.sqlite3')
    if spoof=='sim_to_official':p=G4R2SimulatedProvider(l,client.app.state,'http://127.0.0.1:19999');p.endpoint=policy.ENDPOINT
    else:p=real_provider(ledger(tmp_path/'real-spoof.sqlite3','REAL_LLM'),client.app.state,monkeypatch);p.mode='SIMULATED'
    with pytest.raises(ProviderError):p.transport_guard()

def test_r2_genuine_hard_gate_refusal_not_fake_verified(client):
    from app.g4r2_science import science_summary
    u=RepeatUnit(seed=43,intent='balanced_comparison')
    parent=rid();child='run_'+'a'*32
    client.app.state.orchestration.create_run({'run_id':parent,'state':'RUNNING','nodes':{},'unit_key':'main:43','seed':43,'science_run_id':child})
    client.app.state.orchestration.create_run({'run_id':child,'state':'SUCCEEDED','verification_status':'VERIFIED','nodes':{str(i):{'state':'SUCCEEDED'} for i in range(9)}})
    with pytest.raises(ProviderError,match='FAKE_VERIFIED'):science_summary(client.app.state,parent,child,u)

def test_r2_real_cancellation_sibling_protected(client,monkeypatch):
    s,t=session();client.app.state.g4r2_approval=s;child='run_'+'b'*32;s.protected_runs.add(child);calls=[]
    monkeypatch.setattr(client.app.state.controller,'cancel',lambda r:calls.append(r) or {})
    assert client.post('/api/runs/'+child+'/cancel',headers=headers()).status_code==401
    assert calls==[]
    assert client.post('/api/runs/'+child+'/cancel',headers=headers(t)).status_code==200

@pytest.mark.parametrize('review_case',['accept','forged','recommend'])
def test_r2_new_seed43_science_end_to_end_simulated(local,client,tmp_path,monkeypatch,review_case):
    old,_,state=local;s,t=session();client.app.state.g4r2_approval=s
    l=ledger(tmp_path/'science.sqlite3');p=G4R2SimulatedProvider(l,client.app.state,old.endpoint);service=G4R2Service(client.app.state,l,p)
    client.app.state.g4r2_service=service;state['proposal']={**plan(43),'strategy':'dynamic','verification':'verify_and_repair'}
    original=p.complete
    async def complete(run_id,role,deadline):
        if role=='reviewer':
            _,_,summary,_=p.body(run_id,role)
            state['proposal']={'decision':'accept_evidence','evidence_ids':[summary['evidence_ids'][0]],'reason':'metrics_consistent'}
            if review_case=='forged':state['proposal']['evidence_ids']=['run_'+'b'*32+':1:'+'b'*64]
            if review_case=='recommend':state['proposal']['decision']='recommend_same_tool_reexecution'
        return await original(run_id,role,deadline)
    monkeypatch.setattr(p,'complete',complete)
    response=client.post('/api/g4r2/main',headers=headers(t),json={'seed':43})
    assert response.status_code==200
    result=response.json();assert result['run']['state']==('SUCCEEDED' if review_case=='accept' else 'FAILED'),result['run'].get('error')
    assert result['mode']=='SIMULATED' and result['run']['verification_status']==('VERIFIED' if review_case=='accept' else 'REJECTED') and result['science']['integrity']['ok']
    assert len(result['science']['run']['nodes'])==9 and state['calls']==2
    receipts=[r['receipt'] for r in result['role_receipts'] if r['kind']=='G4R2RoleResponse']
    assert len(receipts)==(2 if review_case=='accept' else 1) and all(r['mode']=='SIMULATED' and r['test_label']=='TEST_ONLY' and not r['official_response_received'] for r in receipts)
    assert result['run']['science_run_id'] in s.protected_runs and t not in json.dumps(result)
    assert client.post('/api/g4r2/main',headers=headers(t),json={'seed':43}).status_code==409
    assert state['calls']==2

def test_r2_actual_science_hard_gate_rejects_and_stops_before_review(local,client,tmp_path,monkeypatch):
    from app.g4r2_service import compile_proposal as original
    old,_,state=local;s,t=session();client.app.state.g4r2_approval=s;l=ledger(tmp_path/'reject.sqlite3')
    p=G4R2SimulatedProvider(l,client.app.state,old.endpoint);client.app.state.g4r2_service=G4R2Service(client.app.state,l,p);state['proposal']=plan(43)
    def injected(plan,controller):
        spec=original(plan,controller)
        binding=next(n for n in spec.nodes if n.id=='leakage').inputs['features']
        binding.value=[*binding.value,'TWF']  # TEST_ONLY copy, never mutate the global FEATURES list.
        return spec
    monkeypatch.setattr('app.g4r2_service.compile_proposal',injected)
    response=client.post('/api/g4r2/main',headers=headers(t),json={'seed':43})
    assert response.status_code==200
    value=response.json();assert value['run']['state']=='FAILED' and value['science']['run']['verification_status']=='REJECTED'
    assert state['calls']==1 and value['mode']=='SIMULATED' and value['test_label']=='TEST_ONLY'
    assert not any(a['tool']=='model_trainer' for a in value['science']['artifacts'])
    assert l.snapshot()['stop_reason'] and l.snapshot()['tokens_reserved']==7978

def test_r2_connection_failure_is_unknown_billing_no_retry(client,tmp_path):
    import socket
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    l=ledger(tmp_path/'connect.sqlite3');p=G4R2SimulatedProvider(l,client.app.state,f'http://127.0.0.1:{port}');s,t=session();r=rid();l.reserve('main:43',r)
    async def run():
        with s.action('start') as g:g.bind(r);return await p.complete(r,'planner',time.monotonic()+5)
    with pytest.raises(ProviderError):asyncio.run(run())
    assert l.snapshot()['sent_or_unknown']==1 and l.snapshot()['tokens_reserved']==7978
    with pytest.raises(ProviderError):asyncio.run(run())

def test_r2_missing_key_after_review_gate_still_no_reservation(client,tmp_path,monkeypatch):
    s,t=session();client.app.state.g4r2_approval=s;l=ledger(tmp_path/'nokey.sqlite3','REAL_LLM');p=real_provider(l,client.app.state,monkeypatch)
    monkeypatch.setattr('app.g4r2_provider.phase_gate',lambda:None)
    monkeypatch.delenv('VERIFLOW_G4R2_API_KEY',raising=False)
    client.app.state.g4r2_service=G4R2Service(client.app.state,l,p)
    r=client.post('/api/g4r2/main',headers=headers(t),json={'seed':43})
    assert r.status_code==409 and r.json()['error']['code']=='KEY_NOT_CONFIGURED'
    assert l.snapshot()['requests_reserved']==0 and client.app.state.orchestration.runs()==[]

def test_r2_concurrent_and_cancel_stops_batch(local,client,tmp_path,monkeypatch):
    import threading
    old,_,state=local;s,t=session();client.app.state.g4r2_approval=s;l=ledger(tmp_path/'cancel.sqlite3')
    p=G4R2SimulatedProvider(l,client.app.state,old.endpoint);service=G4R2Service(client.app.state,l,p);client.app.state.g4r2_service=service
    entered=threading.Event()
    async def pending(*args):
        entered.set();await asyncio.sleep(30)
    monkeypatch.setattr(p,'complete',pending)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(client.post,'/api/g4r2/main',headers=headers(t),json={'seed':43})
        assert entered.wait(5)
        r=next(iter(service.tasks));before=l.snapshot()
        assert client.post('/api/g4r2/main',headers=headers(t),json={'seed':44}).status_code==409
        assert client.post('/api/g4r2/'+r+'/cancel',headers=headers()).status_code==401
        assert before==l.snapshot()
        assert client.post('/api/g4r2/'+r+'/cancel',headers=headers(t)).status_code==200
        result=first.result(timeout=10).json()
    assert result['run']['state']=='FAILED' and l.snapshot()['stop_reason']
    assert l.snapshot()['tokens_reserved']==7978 and state['calls']==0
    assert client.post('/api/g4r2/main',headers=headers(t),json={'seed':44}).status_code==409

def test_r2_optional_planner_only_no_science_or_reviewer(local,client,tmp_path):
    old,_,state=local;s,t=session();client.app.state.g4r2_approval=s;l=ledger(tmp_path/'extra.sqlite3')
    for seed in range(42,47):completed(l,f'main:{seed}')  # TEST_ONLY setup, not real inference.
    p=G4R2SimulatedProvider(l,client.app.state,old.endpoint);client.app.state.g4r2_service=G4R2Service(client.app.state,l,p)
    state['proposal']={**plan(42),'reason':'recall_quality'}
    response=client.post('/api/g4r2/extra',headers=headers(t),json={'intent':'recall_quality'})
    assert response.status_code==200
    r=response.json();assert r['run']['state']=='SUCCEEDED' and r['run']['verification_status']=='PLANNER_ONLY_NOT_SCIENCE_VERIFIED'
    assert set(r['run']['nodes'])=={'planner'} and 'science' not in r and state['calls']==1
    assert client.post('/api/g4r2/extra',headers=headers(t),json={'intent':'recall_quality'}).status_code==409

def test_r2_review_context_cannot_be_supplied_by_caller(client,tmp_path):
    l=ledger(tmp_path/'review.sqlite3');p=G4R2SimulatedProvider(l,client.app.state,'http://127.0.0.1:19999');r=rid();l.reserve('main:43',r)
    with pytest.raises(ProviderError,match='NO_TRUSTED_REVIEW'):p.body(r,'reviewer')

def test_r2_expiry_between_roles_preserves_preallocation(local,client,tmp_path):
    old,_,state=local;s,t=session();now=[policy.START+60];l=G4R2Ledger(tmp_path/'expiry.sqlite3','SIMULATED',clock=lambda:now[0])
    p=G4R2SimulatedProvider(l,client.app.state,old.endpoint);state['proposal']=plan(43);r=rid();l.reserve('main:43',r)
    async def run():
        with s.action('start') as g:
            g.bind(r);await p.complete(r,'planner',time.monotonic()+5)
            now[0]=policy.EXPIRES
            with pytest.raises(ProviderError,match='AUTHORIZATION_EXPIRED'):await p.complete(r,'reviewer',time.monotonic()+5)
    asyncio.run(run());assert state['calls']==1 and l.snapshot()['tokens_reserved']==7978

def test_r2_ready_and_release_are_bound_to_actual_hashes(tmp_path,monkeypatch):
    import os
    monkeypatch.setattr(policy,'OUT',tmp_path)
    source=policy.ROOT/'backend/app/g4r2_policy.py'
    data={'tests_passed':True,'authorization_sha256':policy.SHA,'source_sha256':{'backend/app/g4r2_policy.py':hashlib.sha256(source.read_bytes()).hexdigest()},'budget':policy.dryrun()}
    ready=tmp_path/'READY.json';ready.write_text(json.dumps(data),encoding='utf-8')
    ready_sha=policy.verify_ready()
    with pytest.raises(ProviderError,match='AWAITING_CHATGPT_PREKEY_REVIEW'):policy.phase_gate(local_switch=False)
    review=tmp_path/'TEST_ONLY_REVIEW.md';review.write_text('TEST_ONLY not an actual Gate review',encoding='utf-8')
    release=tmp_path/'CHATGPT_PREKEY_RELEASE.json'
    value={'status':'PASS','authorization_id':policy.ID,'ready_sha256':ready_sha,
        'review_path':review.relative_to(policy.ROOT).as_posix(),'review_sha256':hashlib.sha256(review.read_bytes()).hexdigest()}
    release.write_text(json.dumps(value),encoding='utf-8');policy.phase_gate(local_switch=False)
    monkeypatch.delenv('G4R2_BACKEND_STARTED',raising=False)
    with pytest.raises(ProviderError,match='LOCAL_START_REQUIRED'):policy.phase_gate()
    value['ready_sha256']='0'*64;release.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ProviderError):policy.phase_gate(local_switch=False)
    data['source_sha256']['backend/app/g4r2_policy.py']='0'*64;ready.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ProviderError,match='SOURCE_CHANGED'):policy.verify_ready()

def test_r2_cors_and_no_origin_cli_with_explicit_approval(client):
    s,t=session();client.app.state.g4r2_approval=s
    h={'Origin':'http://127.0.0.1:15173','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':HEADER}
    response=client.options('/api/g4r2/main',headers=h)
    assert response.status_code==200 and HEADER in response.headers['access-control-allow-headers'].lower()
    h['Origin']='https://evil.example';assert client.options('/api/g4r2/main',headers=h).status_code==400
    # Correct approval without Origin passes authentication, then safely stops at Phase P service gate.
    r=client.post('/api/g4r2/main',headers=headers(t),json={'seed':43})
    assert r.status_code==403 and r.json()['error']['code']=='AWAITING_CHATGPT_PREKEY_REVIEW'

@pytest.mark.parametrize('body',[{'seed':43.0},{'seed':'43'},{'seed':41},{'seed':47},{'seed':43,'intent':'latency'},{'seed':43,'api_key':'TEST_ONLY'}])
def test_r2_main_body_cannot_override_matrix(client,body):
    s,t=session();client.app.state.g4r2_approval=s
    r=client.post('/api/g4r2/main',headers=headers(t),json=body)
    assert r.status_code==422 and client.app.state.orchestration.runs()==[]

def test_r2_ready_rejects_changed_body_within_token_caps(tmp_path,monkeypatch):
    monkeypatch.setattr(policy,'OUT',tmp_path)
    source=policy.ROOT/'backend/app/g4r2_policy.py'
    data={'tests_passed':True,'authorization_sha256':policy.SHA,'source_sha256':{'backend/app/g4r2_policy.py':hashlib.sha256(source.read_bytes()).hexdigest()},'budget':policy.dryrun()}
    (tmp_path/'READY.json').write_text(json.dumps(data),encoding='utf-8')
    original=policy.repeat_body
    def changed(*args,**kwargs):
        b=original(*args,**kwargs);b['messages'][0]['content']+=' TEST_ONLY';return b
    monkeypatch.setattr(policy,'repeat_body',changed)
    assert policy.dryrun()['totals']['tokens']<50000
    with pytest.raises(ProviderError,match='BODY_OR_BUDGET_CHANGED'):policy.verify_ready()

def test_r2_real_provider_cannot_reset_budget_with_another_real_ledger(tmp_path):
    l=ledger(tmp_path/'unrelated-real.sqlite3','REAL_LLM')
    with pytest.raises(ProviderError,match='PRODUCTION_LEDGER_REQUIRED'):G4R2Provider(l,None)
    assert l.snapshot()['requests_reserved']==0
