"""TEST_ONLY approval regression; no real Key, official requests or budget use."""
import asyncio,json,time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import httpx,pytest
from test_deepseek import local
from app.deepseek_approval import ApprovalSession,HEADER,execution_grant
from app.deepseek_authorization import START,EXPIRES,RealBudgetLedger
from app.deepseek_real_provider import RealDeepSeekProvider,SimulatedAuthorizedProvider
from app.deepseek_trial import TrialService
from app.llm import ProviderError

@pytest.fixture(autouse=True)
def no_official_http(monkeypatch):
    original=httpx.AsyncClient.send
    async def send(self,request,*args,**kwargs):
        assert request.url.scheme=='http' and request.url.host=='127.0.0.1','TEST_ONLY_LOOPBACK_REQUIRED'
        return await original(self,request,*args,**kwargs)
    monkeypatch.setattr(httpx.AsyncClient,'send',send)

@pytest.fixture
def protected(client):
    session,token=ApprovalSession.issue(wall=lambda:START+60)
    client.app.state.trial_approval=session
    class Stub:
        calls=0;tasks={}
        async def start(self,intent):self.calls+=1;return {'mode':'TEST_ONLY'}
    stub=Stub();client.app.state.real_trial=stub
    return client,session,token,stub

def headers(token=None,**changes):
    value={'Host':'127.0.0.1:18000'}
    if token:value[HEADER]=token
    return {**value,**changes}

def untouched(client,stub):
    assert stub.calls==0 and client.app.state.orchestration.runs()==[]

@pytest.mark.parametrize('path',['/api/deepseek/trial/start','/api/deepseek/trial/run_test/cancel'])
@pytest.mark.parametrize('case',['missing','wrong','expired','old_process','wrong_host','wrong_origin','null_origin','no_origin_no_token','query','duplicate_header','duplicate_host','duplicate_origin','token_in_body'])
def test_sec_denials_before_run_budget_key(protected,path,case):
    api,session,token,stub=protected;h=headers(token)
    body={'intent':'比较三种模型'}
    if case in {'missing','no_origin_no_token'}:h.pop(HEADER)
    if case=='wrong':h[HEADER]='0'*64
    if case=='expired':session._deadline=0
    if case=='old_process':api.app.state.trial_approval=ApprovalSession.issue(wall=lambda:START+60)[0]
    if case=='wrong_host':h['Host']='evil.example:18000'
    if case=='wrong_origin':h['Origin']='https://evil.example'
    if case=='null_origin':h['Origin']='null'
    if case=='query':path+='?approval=not-a-token'
    if case=='token_in_body':h.pop(HEADER);body['approval']=token
    if case.startswith('duplicate_'):
        key={'duplicate_header':HEADER,'duplicate_host':'Host','duplicate_origin':'Origin'}[case]
        h=list(h.items())+[(key,'http://localhost:15173' if key=='Origin' else h.get(key,''))]
        if case=='duplicate_origin':h.append(('Origin','http://127.0.0.1:15173'))
    response=api.post(path,headers=h,json=body)
    assert response.status_code in {401,403}
    untouched(api,stub)
    assert token not in response.text

def test_sec_auth_before_invalid_body(protected):
    api,_,_,stub=protected
    assert api.post('/api/deepseek/trial/start',headers=headers(),content='{').status_code==401
    untouched(api,stub)

def test_sec_expiry_between_authentication_and_action(protected):
    api,_,_,stub=protected
    ticks=iter([0.,0.,1800.])
    session,token=ApprovalSession.issue(clock=lambda:next(ticks),wall=lambda:START+60)
    api.app.state.trial_approval=session
    response=api.post('/api/deepseek/trial/start',headers=headers(token),json={})
    assert response.status_code==403
    untouched(api,stub)

@pytest.mark.parametrize('origin',[None,'http://127.0.0.1:15173','http://localhost:15173'])
@pytest.mark.parametrize('host',['127.0.0.1:18000','localhost:18000'])
def test_sec_valid_header_browser_and_cli(protected,origin,host):
    api,_,token,stub=protected;h=headers(token,Host=host)
    if origin:h['Origin']=origin
    response=api.post('/api/deepseek/trial/start',headers=h,json={})
    assert response.status_code==200 and stub.calls==1
    assert token not in response.text

def test_sec_cors_explicit_approval_header(protected):
    api,_,_,stub=protected
    h={'Origin':'http://127.0.0.1:15173','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':HEADER}
    response=api.options('/api/deepseek/trial/start',headers=h)
    assert response.status_code==200 and HEADER in response.headers['access-control-allow-headers'].lower()
    h['Origin']='https://evil.example'
    assert api.options('/api/deepseek/trial/start',headers=h).status_code==400
    untouched(api,stub)

def test_sec_cancel_hijack_and_sibling(protected,monkeypatch):
    api,session,token,stub=protected;id_='run_'+'e'*32
    session.protected_runs.add(id_);calls=[]
    monkeypatch.setattr(api.app.state.controller,'cancel',lambda run:(calls.append(run) or {'mode':'TEST_ONLY'}))
    for path in ('/api/deepseek/trial/'+id_+'/cancel','/api/runs/'+id_+'/cancel'):
        assert api.post(path,headers=headers()).status_code==401
    assert calls==[]
    assert api.post('/api/runs/'+id_+'/cancel',headers=headers(token)).status_code==200
    assert calls==[id_];untouched(api,stub)

def test_sec_session_entropy_restart_pid_cleanup(monkeypatch):
    now=[10.];session,token=ApprovalSession.issue(clock=lambda:now[0],wall=lambda:START+60)
    other,other_token=ApprovalSession.issue(wall=lambda:START+60)
    assert len(token)==64 and token!=other_token and token not in repr(session.__dict__)
    with pytest.raises(ProviderError):other.authenticate(token)
    session.authenticate(token);now[0]=1810.
    with pytest.raises(ProviderError):session.authenticate(token)
    other.close()
    with pytest.raises(ProviderError):other.authenticate(other_token)
    fresh,new=ApprovalSession.issue(wall=lambda:START+60)
    monkeypatch.setattr('app.deepseek_approval.os.getpid',lambda:-1)
    with pytest.raises(ProviderError):fresh.authenticate(new)

def test_sec_grant_scope_expiry_and_no_reuse():
    now=[10.];session,token=ApprovalSession.issue(clock=lambda:now[0],wall=lambda:START+60)
    with session.action('start') as grant:
        grant.bind('r')
        with pytest.raises(ProviderError):execution_grant('other','planner')
        execution_grant('r','planner',consume=True)
        with pytest.raises(ProviderError):execution_grant('r','planner')
        now[0]=1810.
        with pytest.raises(ProviderError):execution_grant('r','reviewer')
    with pytest.raises(ProviderError):execution_grant('r','reviewer')
    expiry,_=ApprovalSession.issue(wall=lambda:EXPIRES)
    with pytest.raises(ProviderError):expiry.check()

def test_sec_service_direct_call_rejected_before_ledger():
    class ForbiddenLedger:
        def check(self):pytest.fail('UNAUTHORIZED_LEDGER_ACCESS')
    service=TrialService(SimpleNamespace(),ForbiddenLedger(),SimpleNamespace(mode='REAL_LLM'))
    with pytest.raises(ProviderError,match='TRIAL_APPROVAL_REQUIRED'):asyncio.run(service.start('比较三种模型'))

@pytest.mark.parametrize('method',['complete','exchange','prepare','get_secret'])
def test_sec_real_provider_direct_bypass_rejected(method,monkeypatch):
    class ForbiddenLedger:
        mode='REAL_LLM'
        def check(self):pytest.fail('UNAUTHORIZED_LEDGER_ACCESS')
    provider=RealDeepSeekProvider(ForbiddenLedger())
    monkeypatch.setenv('VERIFLOW_LLM_API_KEY','TEST_ONLY_NOT_A_REAL_KEY')
    with pytest.raises(ProviderError,match='TRIAL_APPROVAL_REQUIRED'):
        if method=='prepare':provider.prepare()
        elif method=='get_secret':provider.get_secret()
        elif method=='complete':asyncio.run(provider.complete('r','planner',{'intent':'latency'},time.monotonic()+60))
        else:asyncio.run(provider.exchange('r','planner',{}, {},100,'TEST_ONLY',time.monotonic()+60))

@pytest.mark.parametrize('case',['simulated_to_official','real_mode_spoof'])
def test_sec_mode_label_cannot_open_official_network(tmp_path,case):
    l=RealBudgetLedger(tmp_path/'trial.sqlite3','SIMULATED',clock=lambda:START+60)
    if case=='simulated_to_official':
        provider=SimulatedAuthorizedProvider(l,'http://127.0.0.1:19999');provider.endpoint='https://api.deepseek.com/chat/completions'
    else:
        l.mode='REAL_LLM';provider=RealDeepSeekProvider(l);provider.mode='SIMULATED'
    with pytest.raises(ProviderError,match='ENDPOINT_REJECTED'):
        asyncio.run(provider.exchange('r','planner',{}, {},100,'',time.monotonic()+60))
    assert l.snapshot()['requests_reserved']==0

def test_sec_keyless_authorized_route_no_budget(protected,tmp_path,monkeypatch):
    api,_,token,_=protected
    l=RealBudgetLedger(tmp_path/'real.sqlite3','REAL_LLM',clock=lambda:START+60)
    monkeypatch.setattr('app.deepseek_real_provider.startup_guard',lambda:None)
    monkeypatch.delenv('VERIFLOW_LLM_API_KEY',raising=False)
    api.app.state.real_trial=TrialService(api.app.state,l,RealDeepSeekProvider(l))
    response=api.post('/api/deepseek/trial/start',headers=headers(token),json={})
    assert response.status_code==409 and response.json()['error']['code']=='KEY_NOT_CONFIGURED'
    assert l.snapshot()['requests_reserved']==0 and api.app.state.orchestration.runs()==[]

def test_sec_valid_simulated_workflow_concurrent_and_duplicate(local,protected,tmp_path,monkeypatch):
    old,_,state=local;api,session,token,_=protected
    l=RealBudgetLedger(tmp_path/'sim.sqlite3','SIMULATED',clock=lambda:START+60)
    provider=SimulatedAuthorizedProvider(l,old.endpoint);original=provider.complete
    import threading
    release=threading.Event();entered=threading.Event()
    async def complete(run_id,role,summary,deadline):
        if role=='planner':
            entered.set()
            while not release.is_set():await asyncio.sleep(.01)
        else:state['proposal']={'decision':'accept_evidence','evidence_ids':[summary['evidence_ids'][0]],'reason':'metrics_consistent'}
        return await original(run_id,role,summary,deadline)
    monkeypatch.setattr(provider,'complete',complete)
    api.app.state.real_trial=TrialService(api.app.state,l,provider)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(api.post,'/api/deepseek/trial/start',headers=headers(token),json={'intent':'快速基线检查'})
        try:
            assert entered.wait(5)
            before=l.snapshot();runs=len(api.app.state.orchestration.runs())
            second=api.post('/api/deepseek/trial/start',headers=headers(token),json={})
            assert second.status_code==409 and second.json()['error']['code']=='TRIAL_ALREADY_RUNNING'
            denied=api.post('/api/deepseek/trial/start',headers=headers(),json={})
            assert denied.status_code==401
            assert l.snapshot()==before and len(api.app.state.orchestration.runs())==runs and state['calls']==0
        finally:release.set()
        response=first.result(timeout=90)
    assert response.status_code==200
    result=response.json();assert result['run']['state']=='SUCCEEDED' and result['run']['verification_status']=='VERIFIED'
    assert result['run']['llm_inference']=='SIMULATED' and state['calls']==2 and result['role_integrity']['ok']
    assert result['run']['science_run_id'] in session.protected_runs
    duplicate=api.post('/api/deepseek/trial/start',headers=headers(token),json={})
    assert duplicate.status_code==409 and duplicate.json()['error']['code']=='BUDGET_EXHAUSTED'
    assert state['calls']==2 and token not in json.dumps(result)

def test_sec_noninteractive_bootstrap_never_issues_token(monkeypatch):
    from app import deepseek_local_server as bootstrap
    monkeypatch.setattr(bootstrap.sys,'stdin',SimpleNamespace(isatty=lambda:False))
    with pytest.raises(RuntimeError,match='LOCAL_INTERACTIVE_CONSOLE_REQUIRED'):bootstrap.main()
