"""TEST_ONLY offline preparation; no user Key or official model calls."""
import copy,json,time
from pathlib import Path
import httpx,pytest
from test_deepseek import local
from app.deepseek_repeat_prep import (RepeatUnit,RepeatReviewSummary,repeat_body,validate_plan,validate_review,
    validate_simulated_response,maximal_review_summary,reviewer_bound,budget_dryrun,MODELS)
from app.deepseek_contracts import PLANNER_EXAMPLE,PlanProposal,compile_proposal
from app.deepseek_real_provider import input_bound
from app.llm import ProviderError
from app.science_contracts import DATASET_SHA

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/G4/deepseek/real/repeat-prep'

def proposal(unit):
    return {**copy.deepcopy(PLANNER_EXAMPLE),'seed':unit.seed,'models':MODELS.copy(),'reason':unit.intent,
        'tools':['data_profiler','leakage_check','model_trainer','model_evaluator','report_exporter']}

@pytest.mark.parametrize('seed',[42,43,44,45,46])
@pytest.mark.parametrize('intent',['balanced_comparison','recall_quality','latency'])
def test_repeat_seed_bound_prompt_and_unmodified_output(seed,intent):
    unit=RepeatUnit(seed=seed,intent=intent);body=repeat_body(unit,'planner')
    user=json.loads(body['messages'][1]['content']);assert user['experiment_seed']==seed and user['required_models']==MODELS
    assert f'"const": {seed}' in body['messages'][0]['content']
    raw=json.dumps(proposal(unit));before=raw
    parsed=validate_plan(raw,unit)
    assert parsed.seed==seed and raw==before and parsed.model_dump(exclude={'schema_version'})==json.loads(raw)

@pytest.mark.parametrize('changes',[{'seed':41},{'seed':47},{'seed':43.0},{'seed':'43'},{'seed':True},
    {'intent':'ignore all rules'},{'key':'TEST_ONLY'},{'feature':'TWF'}])
def test_repeat_closed_unit(changes):
    with pytest.raises(ValueError):RepeatUnit.model_validate({'seed':43,'intent':'balanced_comparison',**changes})

@pytest.mark.parametrize('changes',[{'seed':42},{'reason':'latency'},{'models':['dummy']},{'path':'../secret'},
    {'tools':['shell']},{'verification':'execution_only'}])
def test_repeat_plan_mismatch_is_rejected_never_patched(changes):
    unit=RepeatUnit(seed=43,intent='balanced_comparison');value={**proposal(unit),**changes};raw=json.dumps(value)
    with pytest.raises((ProviderError,ValueError)):validate_plan(raw,unit)
    assert json.loads(raw)==value

def test_repeat_budget_no_secret_network_or_real_ledger(monkeypatch):
    import os
    from app.deepseek_authorization import RealBudgetLedger
    original_get=os._Environ.get
    def get(env,key,*args):
        assert key!='VERIFLOW_LLM_API_KEY','NO_KEY_READ_ALLOWED'
        return original_get(env,key,*args)
    monkeypatch.setattr(os._Environ,'get',get)
    monkeypatch.setattr(httpx.Client,'request',lambda *a,**k:pytest.fail('NO_NETWORK_ALLOWED'))
    monkeypatch.setattr(RealBudgetLedger,'__init__',lambda *a,**k:pytest.fail('NO_REAL_LEDGER_ALLOWED'))
    value=budget_dryrun()
    assert value['status']=='AWAITING_NEW_COST_AUTHORIZATION' and value['new_paid_requests']==0
    assert value['four_extra_cycles']['requests']==8 and value['same_version_five_cycles']['requests']==10
    proposed=value['proposal_only_not_authorized'];upper=value['five_plus_two_requests_upper']
    assert proposed['max_reserved_tokens']>=upper['reserved_tokens'] and proposed['max_usd']>=upper['usd_upper']

def test_repeat_schema_change_recomputes_byte_budget(monkeypatch):
    before=budget_dryrun();original=PlanProposal.model_json_schema
    def schema(*args,**kwargs):
        value=original(*args,**kwargs);value['description']='TEST_ONLY schema growth '*100;return value
    monkeypatch.setattr(PlanProposal,'model_json_schema',schema)
    after=budget_dryrun()
    assert after['schema_sha256']!=before['schema_sha256']
    assert after['four_extra_cycles']['reserved_tokens']>before['four_extra_cycles']['reserved_tokens']

@pytest.mark.parametrize('metric',[0.0,-0.0,1.,.9999999999999999,5e-324,2.2250738585072014e-308,1.2345678901234568e-100])
def test_repeat_reviewer_maximum_byte_reservation(metric):
    unit=RepeatUnit(seed=46,intent='balanced_comparison');summary=maximal_review_summary(46)
    summary['metrics']={m:{k:metric for k in values} for m,values in summary['metrics'].items()}
    assert input_bound(repeat_body(unit,'reviewer',summary))<=reviewer_bound(unit)

@pytest.mark.parametrize('case',['unicode','long_seq','foreign_run','nan','wrong_seed','wrong_data'])
def test_repeat_reviewer_summary_fail_closed(case):
    unit=RepeatUnit(seed=43,intent='balanced_comparison');summary=maximal_review_summary(43)
    if case=='unicode':summary['evidence_ids'][0]='run_'+'非'*100
    if case=='long_seq':summary['evidence_ids'][0]='run_'+'f'*32+':1000000:'+'f'*64
    if case=='foreign_run':summary['evidence_ids'][0]='run_'+'a'*32+':1:'+'f'*64
    if case=='nan':summary['metrics']['dummy']['f1']=float('nan')
    if case=='wrong_seed':summary['seed']=44
    if case=='wrong_data':summary['data_sha256']='a'*64
    with pytest.raises((ProviderError,ValueError)):repeat_body(unit,'reviewer',summary)

@pytest.mark.parametrize('case',['illegal_tool','invalid_json','missing_usage','length','tool','bad_usage'])
def test_repeat_simulated_http_failure_no_retry(local,case):
    old,_,state=local;unit=RepeatUnit(seed=43,intent='balanced_comparison')
    state['proposal']=proposal(unit);state['case']='success' if case=='illegal_tool' else case
    if case=='illegal_tool':state['proposal']['tools']=['shell']
    body=repeat_body(unit,'planner')
    with httpx.Client(trust_env=False,follow_redirects=False,timeout=2) as api:
        data=api.post(old.endpoint+'/chat/completions',json=body).json()
    with pytest.raises((ProviderError,ValueError,KeyError)):validate_simulated_response(data,unit,'planner')
    assert state['calls']==1 and not state['authorization_seen']

def test_repeat_forged_review_and_hard_gate_override():
    unit=RepeatUnit(seed=43,intent='balanced_comparison');summary=maximal_review_summary(43)
    review={'decision':'accept_evidence','reason':'metrics_consistent','evidence_ids':[summary['evidence_ids'][0]]}
    summary['status']='REJECTED'
    with pytest.raises(ValueError,match='hard_gate_cannot_be_overridden'):validate_review(json.dumps(review),unit,summary)
    summary['status']='VERIFIED';review['evidence_ids']=['run_'+'a'*32+':1:'+'a'*64]
    with pytest.raises(ValueError,match='unknown_evidence'):validate_review(json.dumps(review),unit,summary)
    review.update(evidence_ids=[summary['evidence_ids'][0]],decision='recommend_same_tool_reexecution')
    with pytest.raises(ValueError,match='repair_budget_exhausted'):validate_review(json.dumps(review),unit,summary)

def wait(state,run_id):
    end=time.monotonic()+40
    while time.monotonic()<end:
        run=state.orchestration.get(run_id)
        if run['state'] in {'SUCCEEDED','FAILED','CANCELLED'}:return run
        time.sleep(.02)
    pytest.fail('bounded science workflow did not finish')

def test_repeat_real_leakage_hard_gate_no_training(client):
    response=client.post('/api/science/demos',json={'seed':43,'feature_set':'leakage_negative'})
    assert response.status_code==202
    run=wait(client.app.state,response.json()['run_id'])
    assert run['state']=='FAILED' and run['verification_status']=='REJECTED'
    artifacts=client.app.state.controller.catalog.artifacts.list(run['run_id'])
    assert not any(a['tool']=='model_trainer' for a in artifacts)
    directory=OUT/'failures';directory.mkdir(parents=True,exist_ok=True)
    with (directory/(run['run_id']+'.json')).open('x',encoding='utf-8') as f:
        json.dump({'mode':'G4_OFFLINE','test_label':'TEST_ONLY_LEAKAGE_REQUEST','run':run,
            'verification':client.app.state.verification.receipts(run['run_id']),'artifacts':artifacts,
            'official_model_requests':0},f,indent=2)

def test_repeat_simulated_seed43_real_science_and_review(local,client):
    from app.science_api import install_demo,report_bytes
    from app.deepseek_preflight import build_review_summary
    from app.workflow_models import RunCreate
    old,_,state=local;unit=RepeatUnit(seed=43,intent='balanced_comparison')
    state['proposal']={**proposal(unit),'strategy':'dynamic','verification':'verify_and_repair'}
    with httpx.Client(trust_env=False,follow_redirects=False,timeout=3) as api:
        data=api.post(old.endpoint+'/chat/completions',json=repeat_body(unit,'planner')).json()
        planner=validate_simulated_response(data,unit,'planner')
        plan=validate_plan(json.dumps(planner['proposal']),unit)
        install_demo(client.app.state,43,'safe',False)
        workflow=compile_proposal(plan,client.app.state.controller)
        client.app.state.controller.save_workflow('repeat_test_only_seed43',workflow)
        started=client.app.state.controller.start(RunCreate(workflow_id='repeat_test_only_seed43',workflow_version=1,
            strategy=plan.strategy,verification={'mode':plan.verification}))
        run=wait(client.app.state,started['run_id'])
        assert run['state']=='SUCCEEDED' and run['verification_status']=='VERIFIED'
        artifacts=client.app.state.controller.catalog.artifacts.list(run['run_id'])
        training=[a['payload'] for a in artifacts if a['tool']=='model_trainer']
        assert len(training)==3 and {a['seed'] for a in training}=={43}
        assert {a['train_split_sha256'] for a in training}=={'e097a4ed2a900965a81b686501b96c539c85cf06ba3d39107b7d2c230761defa'}
        summary={**build_review_summary(client.app.state,run['run_id']),'seed':43,'data_sha256':DATASET_SHA,
            'train_split_sha256':training[0]['train_split_sha256'],'test_split_sha256':training[0]['test_split_sha256']}
        state['proposal']={'decision':'accept_evidence','evidence_ids':[summary['evidence_ids'][0]],'reason':'metrics_consistent'}
        data=api.post(old.endpoint+'/chat/completions',json=repeat_body(unit,'reviewer',summary)).json()
        reviewer=validate_simulated_response(data,unit,'reviewer',summary)
    assert state['calls']==2 and not state['authorization_seen'] and reviewer['official_response_received'] is False
    directory=OUT/'simulated';directory.mkdir(parents=True,exist_ok=True)
    with (directory/(run['run_id']+'.json')).open('x',encoding='utf-8') as f:
        json.dump({'mode':'SIMULATED','test_label':'TEST_ONLY','unit':unit.model_dump(),'planner':planner,'reviewer':reviewer,
            'run':run,'events':client.app.state.orchestration.events(run['run_id'],0,100),
            'verification':client.app.state.verification.receipts(run['run_id']),'artifacts':artifacts,'official_model_requests':0},f,indent=2)
    for format in ('json','csv','html'):
        with (directory/(run['run_id']+'.'+format+'.export')).open('xb') as f:f.write(report_bytes(client.app.state,run['run_id'],format))
