import asyncio,json,sqlite3,time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from test_deepseek import local
from app.deepseek_authorization import RealBudgetLedger,START,EXPIRES,authorization
from app.deepseek_real_provider import RealDeepSeekProvider,SimulatedAuthorizedProvider,input_bound,request_body
from app.deepseek_contracts import PLANNER_EXAMPLE
from app.deepseek_trial import TrialService
from app.llm import ProviderError

def ledger(tmp_path,mode='SIMULATED',clock=None):return RealBudgetLedger(tmp_path/'trial.sqlite3',mode,clock or (lambda:START+60))

@pytest.mark.parametrize('now',[START-1,EXPIRES,EXPIRES+100])
def test_absolute_authorization(now):
    with pytest.raises(ProviderError):authorization(now)

def test_expiry_not_refreshed_and_30s_not_authorization(tmp_path):
    now=[START+60];l=ledger(tmp_path,clock=lambda:now[0]);l.reserve_cycle('run_first',100)
    now[0]+=120
    reopened=ledger(tmp_path,clock=lambda:now[0]);reopened.reserve_cycle('run_second',100)
    assert reopened.snapshot()['requests_reserved']==4
    now[0]=EXPIRES
    with pytest.raises(ProviderError):ledger(tmp_path,clock=lambda:now[0]).reserve_cycle('run_third',100)

def test_namespace_and_no_credit_refresh(tmp_path):
    l=ledger(tmp_path);l.reserve_cycle('r',1000)
    with pytest.raises(ProviderError):RealBudgetLedger(l.path,'REAL_LLM')
    assert ledger(tmp_path).snapshot()['requests_reserved']==2
    with pytest.raises(ProviderError):ledger(tmp_path).reserve_cycle('r',1000)

def test_atomic_pair_budget_and_concurrency(tmp_path):
    l=ledger(tmp_path)
    def reserve(i):
        try:l.reserve_cycle('r'+str(i),4000);return True
        except ProviderError:return False
    with ThreadPoolExecutor(4) as pool:assert sum(pool.map(reserve,range(4)))==1
    assert l.snapshot()['tokens_reserved']==9600

def test_dispatch_crash_and_idempotency(tmp_path):
    l=ledger(tmp_path);l.reserve_cycle('r',100)
    l.dispatch('r','planner','hash',100)
    reopened=ledger(tmp_path)
    with pytest.raises(ProviderError):reopened.dispatch('r','planner','hash',100)
    assert reopened.snapshot()['sent_or_unknown']==1 and reopened.snapshot()['tokens_reserved']==5700

def test_missing_secret_before_network(tmp_path,monkeypatch):
    l=ledger(tmp_path,'REAL_LLM',clock=lambda:time.time());l.reserve_cycle('r',4000)
    p=RealDeepSeekProvider(l)
    monkeypatch.setattr('app.deepseek_real_provider.startup_guard',lambda:None)
    monkeypatch.delenv('VERIFLOW_LLM_API_KEY',raising=False)
    with pytest.raises(ProviderError,match='KEY_NOT_CONFIGURED'):asyncio.run(p.complete('r','planner',{'intent':'latency'},time.monotonic()+60))
    assert l.snapshot()['sent_or_unknown']==0

def test_host_and_mode_rejected(tmp_path):
    l=ledger(tmp_path)
    with pytest.raises(ProviderError):RealDeepSeekProvider(l)
    l=RealBudgetLedger(tmp_path/'real.sqlite3','REAL_LLM')
    p=RealDeepSeekProvider(l);p.endpoint='https://evil.example/chat/completions'
    with pytest.raises(ProviderError,match='ENDPOINT_REJECTED'):asyncio.run(p.complete('r','planner',{'intent':'latency'},time.monotonic()+60))

@pytest.mark.parametrize('case',['401','403','429','500','302','blank','invalid_json','length','large','missing_usage','bad_usage','injection','tool'])
def test_new_transport_failure_no_retry(local,tmp_path,case):
    old,_,state=local;state['case']=case;l=ledger(tmp_path);l.reserve_cycle('r',4000)
    p=SimulatedAuthorizedProvider(l,old.endpoint)
    with pytest.raises(ProviderError):asyncio.run(p.complete('r','planner',{'intent':'latency'},time.monotonic()+60))
    snapshot=l.snapshot();assert snapshot['sent_or_unknown']==1 and snapshot['tokens_reserved']==9600 and state['calls']==1
    assert snapshot['supplier_actual_bill_usd'] is None and not state['authorization_seen']
    with pytest.raises(ProviderError):asyncio.run(p.complete('r','planner',{'intent':'latency'},time.monotonic()+60))
    assert state['calls']==1

def test_simulated_whole_new_orchestrator(local,client,tmp_path,monkeypatch):
    from app.deepseek_contracts import REVIEW_EXAMPLE
    old,_,state=local;l=ledger(tmp_path);provider=SimulatedAuthorizedProvider(l,old.endpoint)
    original=provider.complete
    async def response(run_id,role,summary,deadline):
        if role=='reviewer':state['proposal']={'decision':'accept_evidence','evidence_ids':[summary['evidence_ids'][0]],'reason':'metrics_consistent'}
        return await original(run_id,role,summary,deadline)
    monkeypatch.setattr(provider,'complete',response)
    result=asyncio.run(TrialService(client.app.state,l,provider).start('快速基线检查'))
    assert result['run']['state']=='SUCCEEDED',result
    assert result['run']['llm_inference']=='SIMULATED' and result['budget']['sent_or_unknown']==2
    assert result['role_integrity']['ok'] and state['calls']==2
    assert result['run']['science_run_id']

def test_default_app_never_implicitly_enables_real(client):
    status=client.get('/api/deepseek/trial/status').json()
    assert status['status'] in {'LOCAL_AUTHORIZED_START_REQUIRED','AUTHORIZATION_EXPIRED_OR_NOT_STARTED'}
    assert client.post('/api/deepseek/trial/start',json={}).status_code==409

@pytest.mark.parametrize('cancel',[False,True])
def test_inflight_cancel_timeout_never_refunds(local,tmp_path,cancel):
    old,_,state=local;state['case']='slow';l=ledger(tmp_path);l.reserve_cycle('r',4000)
    p=SimulatedAuthorizedProvider(l,old.endpoint)
    async def run():
        task=asyncio.create_task(p.complete('r','planner',{'intent':'latency'},time.monotonic()+(.05 if not cancel else 60)))
        if cancel:await asyncio.sleep(.03);task.cancel()
        return await task
    with pytest.raises(ProviderError,match='CANCELLED_OR_TIMEOUT'):asyncio.run(run())
    assert l.snapshot()['tokens_reserved']==9600 and l.snapshot()['sent_or_unknown']==1

def test_authorization_expires_between_roles(local,tmp_path):
    old,_,state=local;now=[START+60];l=ledger(tmp_path,clock=lambda:now[0]);l.reserve_cycle('r',4000)
    p=SimulatedAuthorizedProvider(l,old.endpoint)
    result=asyncio.run(p.complete('r','planner',{'intent':'latency'},time.monotonic()+60));assert result['mode']=='SIMULATED'
    now[0]=EXPIRES
    summary={'status':'VERIFIED','evidence_ids':['run_'+'a'*32+':1:receipt'],'repairs_remaining':0,'metrics':{}}
    with pytest.raises(ProviderError,match='AUTHORIZATION_EXPIRED'):asyncio.run(p.complete('r','reviewer',summary,time.monotonic()+60))
    assert state['calls']==1

def test_secret_echo_not_written_to_receipts(local,tmp_path,monkeypatch):
    old,_,state=local;l=ledger(tmp_path);l.reserve_cycle('r',4000)
    p=SimulatedAuthorizedProvider(l,old.endpoint)
    monkeypatch.setattr(p,'get_secret',lambda:'TEST_ONLY_ECHO_SECRET')
    state['proposal']={**PLANNER_EXAMPLE,'reason':'TEST_ONLY_ECHO_SECRET'}
    with pytest.raises(ProviderError,match='SECRET_ECHO_REJECTED'):asyncio.run(p.complete('r','planner',{'intent':'latency'},time.monotonic()+60))
    assert 'TEST_ONLY_ECHO_SECRET' not in json.dumps(l.snapshot())

def test_parent_cannot_use_child_cancel_endpoint(client):
    store=client.app.state.orchestration
    store.create_run({'run_id':'run_'+'c'*32,'state':'PENDING','nodes':{}})
    store.update('run_'+'c'*32,'Started',state='RUNNING')
    response=client.post('/api/runs/run_'+'c'*32+'/cancel')
    assert response.status_code==409 and response.json()['error']['code']=='use_coordinator_cancel_endpoint'
