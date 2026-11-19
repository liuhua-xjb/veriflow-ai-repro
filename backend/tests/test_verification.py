"""G3 independent checks, real tool retries, closed fault adapter and audit."""
import copy
import json
import math
import sqlite3
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.verification_models import VerificationPolicy, VerificationRule, FeedbackPolicy, VerificationResult
from app.validators import validators
from app.verification_store import VerificationStore
from app.workflow_templates import templates, SAMPLE_INPUT, stat, inp, out, field_check


def seed(client):
    for name in ['g3_test_a','g3_test_b']:
        response=client.post('/api/agents',json={'id':name,'name':name,'role':'analyst','capabilities':['analysis'],
            'allowed_tools':['numeric_summary','json_fields'],'concurrency_limit':1})
        assert response.status_code==201,response.text
    for wf in templates(): assert client.post('/api/workflows',json=wf).status_code==201
    for id_,node in [('g3_numeric',stat('summary')),('g3_quality',field_check('quality'))]:
        assert client.post('/api/workflows',json={'workflow_id':id_,'input_schema':{'numbers':{'type':'numbers'},'document':{'type':'object'}},'nodes':[node]}).status_code==201


@pytest.fixture
def g3(tmp_path):
    with TestClient(create_app(tmp_path/'agents.sqlite3',test_only=True)) as client:
        seed(client)
        yield client


def start(client,wf='g3_numeric',fault='none',mode='verify_and_repair',inputs=None,**policy):
    response=client.post('/api/test-only/runs',json={'workflow_id':wf,'workflow_version':1,
        'inputs':SAMPLE_INPUT if inputs is None else inputs,'fault':fault,'verification':{'mode':mode,**policy}})
    assert response.status_code==202,response.text
    return response.json()['run_id']


def finish(client,id_):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        run=client.get('/api/runs/'+id_).json()
        if run['state'] in {'SUCCEEDED','FAILED','CANCELLED'}:
            audit=client.get(f'/api/runs/{id_}/verification').json()
            assert audit['integrity']['ok']
            return run,audit['receipts']
        time.sleep(.005)
    pytest.fail('G3 run did not stop')


def attempts(receipts): return [r for r in receipts if r['kind']=='AttemptFinished']


@pytest.mark.parametrize('configuration',[
    {'abs_tolerance':-1.},{'abs_tolerance':1e90},{'abs_tolerance':float('nan')},{'rel_tolerance':float('inf')},
    {'rel_tolerance':1.},{'version':'2.0.0'},{'mode':'manual_pass'},{'llm_confidence':1.},
    {'rules':[]},{'rules':[{'rule_id':'output_schema'}]*4},
    {'feedback':{'max_repairs':3}},{'feedback':{'max_total_attempts':0}},
    {'feedback':{'token_budget':1}},{'feedback':{'cost_budget':1}},
    {'feedback':{'deadline_seconds':float('nan')}},{'feedback':{'no_progress_limit':1}},
])
def test_policy_bypass_rejected(configuration):
    with pytest.raises(ValidationError): VerificationPolicy.model_validate(configuration)


@pytest.mark.parametrize('rule',[{'rule_id':'llm_opinion'},{'rule_id':'output_schema','required':False},{'rule_id':'output_schema','version':'unknown'}])
def test_rule_contract(rule):
    with pytest.raises(ValidationError): VerificationRule.model_validate(rule)


@pytest.mark.parametrize('values',[[2.,4.,6.],[1e100,-1e100,1.],[5e-324,5e-324],[-3.,-2.,-1.],[1e100]*1000])
def test_independent_decimal_reference(values):
    from app.tools import tools
    output=tools.invoke('numeric_summary',{'values':values},allowed_tools=['numeric_summary'],enabled=True).output
    results=validators.verify('numeric_summary',{'values':values},output,VerificationPolicy())
    assert all(r['passed'] for r in results),results
    for result in results: VerificationResult.model_validate(result)


@pytest.mark.parametrize('mutation',[
    {'count':True},{'count':0},{'count':4},{'mean':float('nan')},{'mean':float('inf')},
    {'extra':1},{'minimum':-1.},{'maximum':7.},{'mean':4.01},{'mean':'4'},
])
def test_hard_gate_bad_summary(mutation):
    output={'count':3,'minimum':2.,'maximum':6.,'mean':4.,**mutation}
    results=validators.verify('numeric_summary',{'values':[2.,4.,6.]},output,VerificationPolicy(abs_tolerance=1e-6,rel_tolerance=1e-9))
    assert any(not r['passed'] for r in results)


def test_tolerance_only_applies_to_mean():
    policy=VerificationPolicy(abs_tolerance=1e-6)
    data={'values':[2.,4.,6.]};output={'count':3,'minimum':2.,'maximum':6.,'mean':4.0000001}
    assert all(r['passed'] for r in validators.verify('numeric_summary',data,output,policy))
    output.update(minimum=2.0000001)
    assert any(not r['passed'] for r in validators.verify('numeric_summary',data,output,policy))


@pytest.mark.parametrize('document,output,reason',[
    ({'mean':4.},{'valid':False,'missing':['mean']},'field_result_mismatch'),
    ({},{'valid':True,'missing':[]},'required_fields_missing'),
    ({},{'valid':False,'missing':['mean']},'required_fields_missing'),
    ({'mean':4.},{'valid':True,'missing':[]},None),
])
def test_quality_recomputed(document,output,reason):
    results=validators.verify('json_fields',{'document':document,'required':['mean']},output,VerificationPolicy())
    codes=[r['failure']['code'] for r in results if not r['passed']]
    assert (reason in codes) if reason else not codes


@pytest.mark.parametrize('mode,fault,state,status,count',[
    ('execution_only','once_mean','SUCCEEDED','UNVERIFIED',1),
    ('verify_only','once_mean','FAILED','REJECTED',1),
    ('verify_and_repair','once_mean','SUCCEEDED','VERIFIED',2),
    ('verify_and_repair','none','SUCCEEDED','VERIFIED',1),
    ('verify_and_repair','permanent_mean','FAILED','REJECTED',2),
])
def test_actual_retry_and_closed_hard_gate(g3,mode,fault,state,status,count):
    run,receipts=finish(g3,start(g3,mode=mode,fault=fault))
    assert (run['state'],run['verification_status'])==(state,status)
    values=attempts(receipts)
    assert len(values)==count and all(r['actual_tool_completed'] for r in values)
    assert len({r['attempt_id'] for r in values})==count
    if count==2:
        assert values[0]['status']=='REJECTED'
        assert values[0]['source_output_digest']!=values[0]['output_digest']
        assert any(r['kind']=='FeedbackAction' and r['action']=='REEXECUTE_SAME_TOOL' for r in receipts)
    if status=='VERIFIED': assert values[-1]['output_digest']==values[-1]['source_output_digest']
    if mode=='execution_only': assert not any(r['kind']=='ValidationResult' for r in receipts)


@pytest.mark.parametrize('feedback,reason,count',[
    ({'max_total_attempts':1},'attempt_budget_exhausted',1),
    ({'no_progress_limit':3},'max_repairs_exhausted',3),
    ({'max_repairs':0},'max_repairs_exhausted',1),
])
def test_budget_and_no_infinite_retry(g3,feedback,reason,count):
    run,receipts=finish(g3,start(g3,fault='permanent_mean',feedback=feedback))
    assert run['state']=='FAILED' and run['error']==reason
    assert len(attempts(receipts))==count
    assert run['verification_budget']['tokens']==run['verification_budget']['cost']==0


def test_missing_real_input_is_unrecoverable(g3):
    run,receipts=finish(g3,start(g3,wf='g3_quality',inputs={**SAMPLE_INPUT,'document':{}}))
    assert run['error']=='unrecoverable_evidence_missing'
    assert len(attempts(receipts))==1
    assert not any(r['kind']=='FeedbackAction' and r['action']=='REEXECUTE_SAME_TOOL' for r in receipts)
    assert run['nodes']['quality']['state']=='FAILED'


@pytest.mark.parametrize('wf',['g2_serial','g2_parallel','g2_condition','g2_loop','g2_subworkflow'])
def test_verified_control_flows_and_skips(g3,wf):
    run,receipts=finish(g3,start(g3,wf=wf,fault='once_mean'))
    assert run['state']=='SUCCEEDED' and run['verification_status']=='VERIFIED'
    checks=[r for r in receipts if r['kind']=='ValidationResult']
    for id_,node in run['nodes'].items():
        if node['kind']!='tool_task' or node['state']=='SKIPPED':
            assert not any(r['node_id']==id_ for r in checks)
    if wf=='g2_parallel':
        assert len(attempts(receipts))==5  # two real attempts per summary and one consume
        assert len({r['attempt_id'] for r in attempts(receipts)})==5
    if wf=='g2_loop':
        assert len(attempts(receipts))==6  # 3 explicit iterations, each 1 repair


def test_failure_propagates_no_child_or_join_fake_checks(g3):
    run,receipts=finish(g3,start(g3,wf='g2_parallel',fault='permanent_mean'))
    assert run['state']=='FAILED'
    assert run['nodes']['consume']['state']=='SKIPPED'
    assert not any(r.get('node_id')=='consume' for r in receipts)


@pytest.mark.parametrize('kind',['cancel','deadline','node_timeout'])
def test_stop_while_running(g3,kind):
    wf={'workflow_id':'stop_'+kind,'input_schema':{'numbers':{'type':'numbers'}},'nodes':[stat('summary',500,timeout_seconds=.02 if kind=='node_timeout' else 2.)]}
    assert g3.post('/api/workflows',json=wf).status_code==201
    id_=start(g3,wf='stop_'+kind,inputs={'numbers':[2.,4.,6.]},feedback={'deadline_seconds':.01} if kind=='deadline' else {})
    if kind=='cancel': assert g3.post('/api/runs/'+id_+'/cancel').status_code==200
    run,receipts=finish(g3,id_)
    assert run['state'] in {'FAILED','CANCELLED'} and run['verification_status']=='REJECTED'
    assert not any(r['kind']=='AttemptFinished' and r['status']=='VERIFIED' for r in receipts)
    assert len(attempts(receipts))<=1


def test_fault_and_override_not_on_production_api(client):
    seed(client)
    request={'workflow_id':'g3_numeric','workflow_version':1,'inputs':SAMPLE_INPUT,'verification':{'mode':'verify_and_repair'}}
    assert client.post('/api/test-only/runs',json={**request,'fault':'once_mean'}).status_code==404
    assert client.post('/api/runs',json={**request,'fault':'once_mean'}).status_code==422
    assert client.post('/api/runs',json={**request,'verification_status':'VERIFIED'}).status_code==422
    assert client.post('/api/runs',json={**request,'verification':{'mode':'verify_only','llm_opinion':'PASS'}}).status_code==422
    assert not client.get('/api/verification/config').json()['test_only_enabled']
    response=client.post('/api/runs',json=request);assert response.status_code==202
    run,receipts=finish(client,response.json()['run_id'])
    assert run['verification_status']=='VERIFIED' and len(attempts(receipts))==1


def test_idempotent_requests_and_conflict(g3):
    request={'workflow_id':'g3_numeric','workflow_version':1,'inputs':SAMPLE_INPUT,'idempotency_key':'TEST_ONLY-key', 'verification':{'mode':'verify_only'}}
    first=g3.post('/api/runs',json=request);second=g3.post('/api/runs',json=request)
    assert first.status_code==second.status_code==202
    assert first.json()['run_id']==second.json()['run_id']
    run,receipts=finish(g3,first.json()['run_id'])
    assert len(attempts(receipts))==1
    assert g3.post('/api/runs',json={**request,'verification':{'mode':'execution_only'}}).status_code==409
    assert 'TEST_ONLY-key' not in json.dumps(receipts)


def test_sensitive_evidence_redacted(g3):
    wf={'workflow_id':'sensitive','input_schema':{'document':{'type':'object','sensitive':True}},'nodes':[field_check('quality')]}
    assert g3.post('/api/workflows',json=wf).status_code==201
    run,receipts=finish(g3,start(g3,wf='sensitive',inputs={'document':{'mean':4.,'private':'TEST_ONLY-secret'}}))
    text=json.dumps(receipts)
    assert 'TEST_ONLY-secret' not in text and 'private' not in text
    assert all(r.get('output_preview')=='[REDACTED]' for r in attempts(receipts))


def test_tamper_detection_append_only_and_reopen(tmp_path):
    path=tmp_path/'verification.sqlite3';store=VerificationStore(path)
    store.append('r','AttemptStarted',node_id='n',attempt_id='n:1')
    store.append('r','AttemptFinished',node_id='n',attempt_id='n:1',status='VERIFIED')
    assert VerificationStore(path).integrity('r')['ok']
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError,match='append_only'): db.execute("DELETE FROM audit")
        with pytest.raises(sqlite3.IntegrityError,match='append_only'): db.execute("UPDATE audit SET kind='forged'")
        db.execute('DROP TRIGGER audit_no_update')
        db.execute("UPDATE audit SET payload=json_set(payload,'$.status','FORGED') WHERE seq=2")
    assert not store.integrity('r')['ok']


def test_sidecar_migration_and_legacy_rollback(tmp_path):
    from app.event_store import EventStore
    from app.registry import AgentRegistry
    from app.runner import RunController
    from app.workflow_models import digest
    legacy=EventStore(tmp_path/'orchestration.sqlite3')
    legacy.create_run({'run_id':'legacy','state':'SUCCEEDED','nodes':{}})
    original=legacy.get('legacy')
    before=(tmp_path/'orchestration.sqlite3').read_bytes()
    store=VerificationStore(tmp_path/'verification.sqlite3')
    VerificationStore(store.path)  # idempotent schema open
    assert (tmp_path/'orchestration.sqlite3').read_bytes()==before
    assert legacy.get('legacy')==original and not store.receipts('legacy')
    # Rollback reads G2 independently of the G3 sidecar; no destructive down migration.
    assert EventStore(legacy.path).get('legacy')==original
    legacy.create_run({'run_id':'interrupted','state':'RUNNING','nodes':{}})
    store.append('interrupted','AttemptStarted',node_id='n',attempt_id='n:1')
    controller=RunController(legacy,AgentRegistry(tmp_path/'agents.sqlite3'),store)
    controller.close()
    assert legacy.get('interrupted')['state']=='FAILED'
    assert any(r.get('reason')=='process_interrupted' for r in store.receipts('interrupted'))
    old=store.receipts('interrupted')
    store.recover(legacy)
    assert store.receipts('interrupted')==old
