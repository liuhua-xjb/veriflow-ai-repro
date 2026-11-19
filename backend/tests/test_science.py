"""G4 real data, real training, hard process limits and offline truthfulness."""
import copy,hashlib,json,math,random,threading,time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.main import create_app
from app.science_contracts import CONTRACTS,FEATURES,EXCLUDED,DATASET_SHA,ModelTrainer,DataProfiler
from app.science_data import load_data,profile,approved_features,source
from app.science_validation import reference_metrics,check_training
from app.science_isolation import isolated,WorkerError
from app.science_artifacts import Artifacts
from app.science_api import install_demo
from app.workflow_models import RunCreate,digest


def finish(client,id_):
    deadline=time.monotonic()+35
    while time.monotonic()<deadline:
        response=client.get('/api/runs/'+id_);assert response.status_code==200
        run=response.json()
        if run['state'] in {'SUCCEEDED','FAILED','CANCELLED'}:return run
        time.sleep(.01)
    pytest.fail('science run failed to terminate')


@pytest.fixture(scope='module')
def trained(tmp_path_factory):
    directory=tmp_path_factory.mktemp('g4-real-science')
    with TestClient(create_app(directory/'agents.sqlite3')) as client:
        response=client.post('/api/science/demos',json={});assert response.status_code==202,response.text
        run=finish(client,response.json()['run_id']);assert run['state']=='SUCCEEDED',run
        yield client,run


def test_official_data_integrity_and_class_distribution():
    rows=load_data();record=source();summary=profile(rows)
    assert record['synthetic'] and record['rows']==len(rows)==10000
    assert record['sha256']==DATASET_SHA
    assert summary['class_distribution']=={'0':9661,'1':339}
    assert set(summary['missing'].values())=={0}
    assert 'UDI' in EXCLUDED and 'TWF' in EXCLUDED
    assert not set(FEATURES)&set(EXCLUDED)


@pytest.mark.parametrize('features',[FEATURES+['Machine failure'],FEATURES+['TWF'],FEATURES+['UDI'],FEATURES+['Product ID'],['target_copy'],FEATURES[::-1],FEATURES[:-1]])
def test_feature_allowlist_rejects_leakage(features):assert not approved_features(features)


@pytest.mark.parametrize('payload',[{'dataset':'../other.csv'},{'dataset':'https://example.com/data.csv'},{'dataset':'ai4i2020','code':'exec(1)'},{'dataset':'ai4i2020','path':'D:/secret'}])
def test_no_arbitrary_data_path_or_code(payload):
    with pytest.raises(ValidationError):DataProfiler.model_validate(payload)


@pytest.mark.parametrize('changes',[{'model':'__import__'},{'seed':0},{'seed':True},{'n_estimators':1000000},{'leakage_id':'../../x'},{'shell':'x'}])
def test_training_parameters_closed(changes):
    with pytest.raises(ValidationError):ModelTrainer.model_validate({'leakage_id':'art_'+'a'*32,'model':'dummy','seed':42,**changes})


def test_dataset_tamper_and_size_rejected(tmp_path,monkeypatch):
    import app.science_data as data
    (tmp_path/'ai4i2020.csv').write_text('forged',encoding='utf-8');monkeypatch.setattr(data,'DATA',tmp_path)
    with pytest.raises(ValueError,match='dataset_integrity_failed'):data.load_data()
    (tmp_path/'ai4i2020.csv').write_bytes(b'x'*1048577)
    with pytest.raises(ValueError,match='dataset_size_limit'):data.load_data()


@pytest.mark.parametrize('scores_mode',['zeros','ones','ties','random'])
def test_independent_metrics_agree_with_sklearn(scores_mode):
    from app.science_worker import calculate
    generator=random.Random(7);labels=[1]*68+[0]*1932
    scores=[0. if scores_mode=='zeros' else 1. if scores_mode=='ones' else generator.choice([.1,.3,.7]) if scores_mode=='ties' else generator.random() for _ in labels]
    predictions=[int(s>.5) for s in scores]
    reference=reference_metrics(labels,predictions,scores);actual=calculate(labels,predictions,scores)
    for key in reference:
        if key=='confusion_matrix':assert reference[key]==actual[key]
        else:assert reference[key]==pytest.approx(actual[key],abs=1e-12)


@pytest.mark.parametrize('operation',['timeout','cancel'])
def test_real_worker_is_terminated(operation):
    signal=threading.Event()
    if operation=='cancel':threading.Timer(.05,signal.set).start()
    with pytest.raises(WorkerError) as caught:
        isolated({'operation':'__TEST_ONLY_SLEEP','test_only':True},signal,time.monotonic()+(.05 if operation=='timeout' else 5.))
    assert caught.value.code==('science_timeout' if operation=='timeout' else 'cancelled')
    assert caught.value.record['terminated'] is True
    assert caught.value.record['job_limits_enforced'] is True
    assert caught.value.record['exit_code'] is not None


def test_actual_three_model_run_and_independent_validators(trained):
    client,run=trained
    assert run['execution_mode']=='deterministic_tool_only' and run['llm_inference']=='NOT_EXECUTED'
    assert run['verification_status']=='VERIFIED'
    records=client.app.state.controller.catalog.artifacts.list(run['run_id'])
    training=[r for r in records if r['tool']=='model_trainer'];assert len(training)==3
    assert {r['payload']['model'] for r in training}=={'dummy','logistic','random_forest'}
    rows=load_data()
    for record in training:
        check_training(record['payload'],rows)
        assert record['payload']['training_ms']>0
        assert record['execution']['job_limits_enforced']
        assert record['execution']['memory_limit_bytes']==1024**3
    audit=client.get('/api/runs/'+run['run_id']+'/verification').json()
    assert audit['integrity']['ok'] and any(r['kind']=='ValidationResult' and r['result']['rule_id']=='science_integrity_and_metrics' for r in audit['receipts'])


@pytest.mark.parametrize('mutation',['overlap','feature','scaler','label','digest'])
def test_training_hard_gate_rejects_tamper(trained,mutation):
    client,run=trained
    data=copy.deepcopy(next(r['payload'] for r in client.app.state.controller.catalog.artifacts.list(run['run_id']) if r['tool']=='model_trainer'))
    if mutation=='overlap':data['test_indices'][0]=data['train_indices'][0]
    if mutation=='feature':data['features'].append('TWF')
    if mutation=='scaler':data['scaler_mean'][0]+=1.
    if mutation=='label':data['labels'][0]=1-data['labels'][0]
    if mutation=='digest':data['train_split_sha256']='0'*64
    with pytest.raises(ValueError):check_training(data,load_data())


def test_metric_forgery_rejected(trained):
    from app.science_validation import validate_artifact
    client,run=trained;store=client.app.state.controller.catalog.artifacts
    record=copy.deepcopy(next(r for r in store.list(run['run_id']) if r['tool']=='model_evaluator'))
    params={'training_id':record['payload']['training_id']};record['payload']['metrics']['f1']=1.1
    with pytest.raises(ValueError,match='metric_mismatch'):validate_artifact('model_evaluator',params,record,store,run['run_id'])


@pytest.mark.parametrize('format',['json','csv','html'])
def test_reports_real_safe_persistent_bytes(trained,format):
    client,run=trained;url=f"/api/science/runs/{run['run_id']}/report/{format}"
    first=client.get(url);second=client.get(url)
    assert first.status_code==second.status_code==200
    assert first.content==second.content and DATASET_SHA.encode() in first.content
    assert first.headers['x-content-type-options']=='nosniff'
    assert b'api_key' not in first.content.lower()
    if format=='json':
        assert first.json()['report']['payload']['llm']=='BLOCKED_NOT_AUTHORIZED'
        assert len(first.json()['report']['payload']['models'])==3


def test_real_leakage_request_fails_without_training(client):
    response=client.post('/api/science/demos',json={'feature_set':'leakage_negative'});assert response.status_code==202,response.text
    run=finish(client,response.json()['run_id'])
    assert run['state']=='FAILED' and run['verification_status']=='REJECTED'
    assert run['nodes']['leakage']['state']=='FAILED'
    assert run['nodes']['train_dummy']['state']=='SKIPPED'
    records=client.app.state.controller.catalog.artifacts.list(run['run_id'])
    assert {r['tool'] for r in records}=={'data_profiler','leakage_check'}
    assert client.get(f"/api/science/runs/{run['run_id']}/report/json").status_code==409


def test_real_llm_is_resource_gated(client):
    status=client.get('/api/science/llm').json()
    assert status['status']=='BLOCKED' and status['authorization'] is False
    assert status['calls']==status['tokens']==status['cost']==0
    response=client.post('/api/science/demos',json={'reasoning':'real_llm'})
    assert response.status_code==409 and response.json()['error']['code']=='real_llm_resource_authorization_required'


def test_artifact_scope_and_tamper(trained,tmp_path):
    client,run=trained;store=client.app.state.controller.catalog.artifacts
    record=store.list(run['run_id'])[0]
    with pytest.raises(OSError):store.load('run_'+'a'*32,record['artifact_id'])
    for id_ in ['../x','/etc/passwd','art_zz']:
        with pytest.raises(ValueError):store.path(run['run_id'],id_)
    clone=Artifacts(tmp_path/'owned-artifacts')
    path=clone.path(run['run_id'],record['artifact_id']);path.parent.mkdir(parents=True)
    forged={**record,'tool':'forged'};path.write_text(json.dumps(forged),encoding='utf-8')
    with pytest.raises(ValueError,match='artifact_integrity_failed'):clone.load(run['run_id'],record['artifact_id'])


def test_permission_denial_cannot_start_worker(client):
    catalog=client.app.state.controller.catalog
    result=catalog.invoke('data_profiler',{'dataset':'ai4i2020'},allowed_tools=[],enabled=True,context={})
    assert not result.ok and result.error=='permission_denied'
    assert client.post('/api/agents',json={'id':'science_denied','name':'Denied','role':'analyst','capabilities':['data_science'],'allowed_tools':[]}).status_code==201
    spec={'workflow_id':'science_denied','nodes':[{'id':'p','kind':'tool_task','tool':'data_profiler','capability':'data_science','agent_id':'science_denied','inputs':{'dataset':{'source':'literal','value':'ai4i2020'}}}]}
    assert client.post('/api/workflows',json=spec).status_code==422


def test_test_only_digest_repair_and_budget(tmp_path):
    with TestClient(create_app(tmp_path/'agents.sqlite3',test_only=True)) as client:
        state=client.app.state
        id_=install_demo(state,42,'safe',False)
        response=client.post('/api/test-only/runs',json={'workflow_id':id_,'workflow_version':1,'fault':'once_artifact_digest','verification':{'mode':'verify_and_repair'}})
        assert response.status_code==202,response.text
        run=finish(client,response.json()['run_id']);assert run['state']=='SUCCEEDED',run
        audit=client.get('/api/runs/'+run['run_id']+'/verification').json()
        repairs=[r for r in audit['receipts'] if r['kind']=='FeedbackAction' and r['action']=='REEXECUTE_SAME_TOOL']
        assert len(repairs)==3 and all(r['node_id'].startswith('eval_') for r in repairs)
        assert len([r for r in audit['receipts'] if r['kind']=='AttemptFinished'])==12
        assert audit['integrity']['ok'] and run['test_label']=='TEST_ONLY'


def test_science_idempotency_no_extra_run(client):
    request={'feature_set':'leakage_negative','idempotency_key':'G4_TEST_ONLY-idempotency'}
    one=client.post('/api/science/demos',json=request);two=client.post('/api/science/demos',json=request)
    assert one.status_code==two.status_code==202 and one.json()['run_id']==two.json()['run_id']
    run=finish(client,one.json()['run_id']);assert run['state']=='FAILED'


def test_unknown_feature_text_rejected_before_artifact_storage(client):
    # Arbitrary metadata must not turn into a new raw-text audit sink.
    catalog=client.app.state.controller.catalog
    result=catalog.invoke('leakage_check',{'profile_id':'art_'+'a'*32,'features':['TEST_ONLY-sensitive-free-text']},
                          allowed_tools=['leakage_check'],enabled=True,context={})
    assert not result.ok and result.error=='invalid_input'
    assert 'TEST_ONLY-sensitive-free-text' not in result.model_dump_json()
