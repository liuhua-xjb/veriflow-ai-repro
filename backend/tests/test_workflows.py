import copy
import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.workflow_templates import templates, SAMPLE_INPUT, stat, edge, inp, literal, out


def seed(client):
    for id_ in ['g2_a','g2_b']:
        assert client.post('/api/agents', json={'id':id_, 'name':id_, 'role':'analyst', 'capabilities':['analysis'],
            'allowed_tools':['numeric_summary','json_fields'], 'concurrency_limit':1}).status_code == 201


@pytest.fixture
def prepared(client):
    seed(client)
    for definition in templates():
        response = client.post('/api/workflows', json=definition)
        assert response.status_code == 201, response.text
    return client


def start(client, workflow_id='g2_parallel', inputs=None, **kwargs):
    response = client.post('/api/runs', json={'workflow_id':workflow_id,'workflow_version':1,
        'inputs': SAMPLE_INPUT if inputs is None else inputs, **kwargs})
    assert response.status_code == 202, response.text
    return response.json()['run_id']


def finish(client, run_id):
    deadline = time.monotonic()+8
    while time.monotonic()<deadline:
        value = client.get('/api/runs/'+run_id).json()
        if value['state'] in {'SUCCEEDED','FAILED','CANCELLED'}: return value
        time.sleep(.005)
    pytest.fail('run did not terminate')


@pytest.mark.parametrize('name', ['g2_serial','g2_parallel','g2_condition','g2_loop','g2_subworkflow'])
def test_five_real_control_flows(prepared, name):
    value = finish(prepared, start(prepared,name))
    assert value['state'] == 'SUCCEEDED', value
    assert value['execution_mode'] == 'deterministic_tool_only'
    assert value['llm_inference'] == 'NOT_EXECUTED'
    assert all(n['state'] in {'SUCCEEDED','SKIPPED'} for n in value['nodes'].values())
    if name == 'g2_parallel':
        a,b = value['nodes']['left'],value['nodes']['right']
        assert value['metrics']['max_concurrency'] == 2
        assert a['agent_id'] != b['agent_id']
        assert max(a['worker_started_at'], b['worker_started_at']) < min(a['finished_at'], b['finished_at'])
        assert value['nodes']['consume']['started_at'] >= value['nodes']['merge']['finished_at']
    if name == 'g2_loop':
        assert len([n for n in value['nodes'] if n.endswith('/summary')]) == 3
        assert value['nodes']['repeat/2/summary']['started_at'] >= value['nodes']['repeat/1/summary']['finished_at']
    if name == 'g2_subworkflow':
        assert value['nodes']['child/1/summary']['parent_node_id'] == 'child/1'
        assert value['subworkflow_snapshots']['g2_body@1']


@pytest.mark.parametrize('numbers,skipped', [([2.,4.,6.],'negative'),([0.,1.,2.],'positive')])
def test_true_false_paths(prepared,numbers,skipped):
    value=finish(prepared,start(prepared,'g2_condition',inputs={**SAMPLE_INPUT,'numbers':numbers}))
    assert value['state']=='SUCCEEDED'
    assert value['nodes'][skipped]['state']=='SKIPPED'


@pytest.mark.parametrize('mutation,code', [
    ('duplicate','duplicate_node'),('dangling','dangling_edge'),('cycle','dag_cycle'),
    ('missing_edge','unmet_data_dependency'),('unknown_input','undeclared_input'),
    ('wrong_type','tool_input_type_mismatch'),('unknown_output','unknown_output_field'),
    ('unknown_agent','unknown_agent'),('gate','invalid_branch_gate'),
])
def test_static_graph_rejections(client,mutation,code):
    seed(client)
    wf=copy.deepcopy(templates()[0]); wf['workflow_id']='invalid_'+mutation
    if mutation=='duplicate': wf['nodes'].append(wf['nodes'][0])
    if mutation=='dangling': wf['edges'].append(edge('missing','summary'))
    if mutation=='cycle': wf['edges'].append(edge('check','summary'))
    if mutation=='missing_edge': wf['edges']=[edge('decide','check')]
    if mutation=='unknown_input': wf['nodes'][0]['inputs']['values']=inp('not_declared')
    if mutation=='wrong_type': wf['nodes'][0]['inputs']['values']=inp('document')
    if mutation=='unknown_output': wf['nodes'][1]['predicate']['left']=out('summary','not_present')
    if mutation=='unknown_agent': wf['nodes'][0]['agent_id']='missing'
    if mutation=='gate': wf['nodes'][2]['gate']['condition']='summary'
    response=client.post('/api/workflows',json=wf)
    assert response.status_code==422, response.text
    assert response.json()['error']['code']==code


@pytest.mark.parametrize('change', [
    {'kind':'shell'}, {'kind':'llm'}, {'tool':'shell'}, {'timeout_seconds':0}, {'priority':True},
    {'predicate':{'op':'python','left':inp('numbers'),'right':'__import__("os")'}},
    {'kind':'bounded_loop','tool':None,'capability':None},
])
def test_schema_rejects_unsafe_nodes(client,change):
    seed(client); wf=copy.deepcopy(templates()[3]); wf['workflow_id']='unsafe'
    wf['nodes'][0].update(change)
    assert client.post('/api/workflows',json=wf).status_code==422


@pytest.mark.parametrize('field,value', [('iterations',0),('iterations',6),('iterations',None)])
def test_loop_limits(prepared,field,value):
    wf=copy.deepcopy(templates()[4]);wf['workflow_id']='invalid_loop';wf['nodes'][0][field]=value
    assert prepared.post('/api/workflows',json=wf).status_code==422


def test_recursion_depth_and_expansion(prepared):
    direct=copy.deepcopy(templates()[5]); direct['workflow_id']='recursive'
    direct['nodes'][0]['workflow']={'workflow_id':'recursive','version':1}
    assert prepared.post('/api/workflows',json=direct).json()['error']['code']=='recursive_subworkflow'
    chain=copy.deepcopy(templates()[5]); chain['workflow_id']='level2'
    assert prepared.post('/api/workflows',json=chain).status_code==201
    chain['workflow_id']='level3';chain['nodes'][0]['workflow']['workflow_id']='level2'
    assert prepared.post('/api/workflows',json=chain).status_code==201
    chain['workflow_id']='level4';chain['nodes'][0]['workflow']['workflow_id']='level3'
    assert prepared.post('/api/workflows',json=chain).json()['error']['code']=='subworkflow_depth_limit'
    big=copy.deepcopy(templates()[3]);big['workflow_id']='big';big['nodes']=[stat('n'+str(i)) for i in range(25)]
    big['outputs']={'mean':out('n0','mean')}
    assert prepared.post('/api/workflows',json=big).status_code==201
    loop=copy.deepcopy(templates()[4]);loop['workflow_id']='too_many';loop['nodes'][0].update(iterations=5,workflow={'workflow_id':'big','version':1})
    assert prepared.post('/api/workflows',json=loop).json()['error']['code']=='expanded_node_limit'


def test_permissions_and_disabled_at_preflight(prepared):
    wf=copy.deepcopy(templates()[3]);wf['workflow_id']='pinned';wf['nodes'][0]['agent_id']='g2_a'
    assert prepared.post('/api/workflows',json=wf).status_code==201
    prepared.patch('/api/agents/g2_a/enabled',json={'enabled':False,'base_version':1})
    response=prepared.post('/api/runs',json={'workflow_id':'pinned','workflow_version':1,'inputs':SAMPLE_INPUT})
    assert response.status_code==422 and response.json()['error']['code']=='no_eligible_agent'
    wf['workflow_id']='capability';wf['nodes'][0]['capability']='not_supported'
    assert prepared.post('/api/workflows',json=wf).status_code==422


def test_versions_persistence_and_conflicts(prepared,tmp_path):
    original=prepared.get('/api/workflows/g2_parallel').json()
    revision={k:v for k,v in templates()[1].items() if k!='workflow_id'}
    revision['base_version']=1
    assert prepared.post('/api/workflows/g2_parallel/versions',json=revision).json()['version']==2
    assert prepared.post('/api/workflows/g2_parallel/versions',json=revision).status_code==409
    assert prepared.post('/api/workflows',json=templates()[1]).status_code==409
    assert prepared.get('/api/workflows/g2_parallel?version=1').json()==original
    assert len(prepared.get('/api/workflows/g2_parallel/versions').json())==2
    assert prepared.get('/api/workflows/missing').status_code==404


def test_runtime_failure_input_and_events(prepared):
    wf=copy.deepcopy(templates()[0]);wf['workflow_id']='runtime_failure'
    wf['nodes']=[{'id':'bad','kind':'tool_task','tool':'json_fields','capability':'analysis',
        'inputs':{'document':inp('document'),'required':literal(['x','x'])}}];wf['edges']=[]
    assert prepared.post('/api/workflows',json=wf).status_code==201
    value=finish(prepared,start(prepared,'runtime_failure'))
    assert value['state']=='FAILED' and value['error']=='invalid_input'
    assert value['nodes']['bad']['state']=='FAILED'
    events=[];after=0
    while True:
        page=prepared.get('/api/runs/'+value['run_id']+'/events',params={'after':after,'limit':2}).json()
        if not page['events']:break
        events+=page['events'];after=page['next_after']
    assert [e['seq'] for e in events]==list(range(1,len(events)+1))
    assert all(e['run_id']==value['run_id'] and e['timestamp'] for e in events)
    assert all(e['attempt_id']=='bad:1' for e in events if e['node_id'])
    assert prepared.get('/api/runs/'+value['run_id']+'/events?limit=101').status_code==422


@pytest.mark.parametrize('mode',['cancel','run_timeout','node_timeout'])
def test_stop_paths(prepared,mode):
    wf=copy.deepcopy(templates()[3]);wf['workflow_id']='stop_'+mode;wf['nodes'][0]['service_delay_ms']=300
    if mode=='node_timeout':wf['nodes'][0]['timeout_seconds']=.01
    if mode=='run_timeout':wf['timeout_seconds']=.01
    assert prepared.post('/api/workflows',json=wf).status_code==201
    run_id=start(prepared,wf['workflow_id'])
    if mode=='cancel':assert prepared.post('/api/runs/'+run_id+'/cancel').status_code==200
    value=finish(prepared,run_id)
    assert value['state']==('CANCELLED' if mode=='cancel' else 'FAILED')
    assert value['error']=={'cancel':'cancel_requested','run_timeout':'run_timeout','node_timeout':'node_timeout'}[mode]
    assert all(n['state'] in {'SUCCEEDED','FAILED','SKIPPED','CANCELLED'} for n in value['nodes'].values())
    assert prepared.post('/api/runs/'+run_id+'/cancel').status_code==409


def test_snapshot_parallel_caps_and_serial_baseline(prepared):
    run_id=start(prepared)
    prepared.patch('/api/agents/g2_a/enabled',json={'enabled':False,'base_version':1})
    value=finish(prepared,run_id)
    assert value['state']=='SUCCEEDED'
    assert next(a for a in value['agent_snapshots'] if a['id']=='g2_a')['version']==1
    for a in ['g2_a','g2_b']:
        assert all(n.get('agent_version')==1 for n in value['nodes'].values() if n.get('agent_id')==a)
    serial=finish(prepared,start(prepared,strategy='fixed_serial'))
    assert serial['state']=='SUCCEEDED' and serial['metrics']['max_concurrency']==1


def test_sensitive_inputs_not_stored(prepared):
    values={**SAMPLE_INPUT,'document':{'api_key':'synthetic-secret-must-not-persist','Authorization':'Bearer synthetic'}}
    value=finish(prepared,start(prepared,'g2_serial',values))
    assert value['state']=='SUCCEEDED'
    events=prepared.get('/api/runs/'+value['run_id']+'/events').json()
    text=json.dumps([value,events])
    assert 'synthetic-secret-must-not-persist' not in text and 'Bearer synthetic' not in text
    assert value['input_digest']


def test_reopen_history_and_orphan(tmp_path):
    from app.event_store import EventStore
    from app.registry import AgentRegistry
    from app.runner import RunController
    path=tmp_path/'runs.sqlite3';store=EventStore(path)
    store.create_run({'run_id':'orphan','state':'RUNNING','nodes':{'task':{'state':'RUNNING'}},'input_digest':'digest-only'})
    reopened=EventStore(path);controller=RunController(reopened,AgentRegistry(tmp_path/'agents.sqlite3'))
    try:
        assert reopened.get('orphan')['state']=='FAILED'
        assert reopened.get('orphan')['error']=='process_interrupted'
        assert reopened.events('orphan')[-1]['kind']=='ProcessInterrupted'
    finally:controller.close()


@pytest.mark.parametrize('value', [float('nan'),[],{'nested':{'too':'deep'}},'not-numbers'])
def test_invalid_run_values(prepared,value):
    # ASGI raw JSON allows the same malformed numeric body a local HTTP caller could send.
    response=prepared.post('/api/runs',content=json.dumps({'workflow_id':'g2_parallel','workflow_version':1,
        'inputs':{**SAMPLE_INPUT,'numbers':value}}),headers={'Content-Type':'application/json'})
    assert response.status_code==422


def test_priority_weights_and_secret_literals(prepared):
    wf=copy.deepcopy(templates()[3]);wf['workflow_id']='priorities';wf['outputs']={}
    wf['nodes']=[stat('a_low',100,priority=0),stat('z_high',100,priority=99)]
    assert prepared.post('/api/workflows',json=wf).status_code==201
    for weight,expected in [(1.0,'z_high'),(0.0,'a_low')]:
        value=finish(prepared,start(prepared,'priorities',scheduler={'priority_weight':weight}))
        trace=prepared.get('/api/runs/'+value['run_id']+'/events').json()['events']
        assigned=[e for e in trace if e['kind']=='AgentAssigned']
        assert assigned[0]['node_id']==expected
        assert assigned[0]['metadata']['config_digest']==value['scheduler_config_digest']
    secret=copy.deepcopy(templates()[3]);secret['workflow_id']='literal_secret'
    secret['nodes']=[{'id':'bad','kind':'tool_task','tool':'json_fields','capability':'analysis',
        'inputs':{'document':literal({'api_key':'synthetic-secret'}),'required':literal(['x'])}}];secret['outputs']={}
    response=prepared.post('/api/workflows',json=secret)
    assert response.status_code==422 and 'synthetic-secret' not in response.text


def test_global_run_admission_and_agent_caps(prepared):
    ids=[start(prepared,'g2_loop') for _ in range(4)]
    fifth=prepared.post('/api/runs',json={'workflow_id':'g2_loop','workflow_version':1,'inputs':SAMPLE_INPUT})
    assert fifth.status_code==409 and fifth.json()['error']['code']=='run_capacity_limit'
    values=[finish(prepared,id_) for id_ in ids]
    assert all(v['state']=='SUCCEEDED' for v in values)
    intervals=[]
    for v in values:
        for n in v['nodes'].values():
            if n.get('agent_id'):
                intervals.extend([(n['worker_started_at'],1,n['agent_id']),(n['finished_at'],-1,n['agent_id'])])
    counts={}
    for _,delta,id_ in sorted(intervals):
        counts[id_]=counts.get(id_,0)+delta
        assert 0<=counts[id_]<=1


def test_exclusive_owner_and_full_orphan_scan(tmp_path):
    from app.event_store import EventStore
    from app.registry import AgentRegistry
    from app.runner import RunController
    store=EventStore(tmp_path/'runs.sqlite3');agents=AgentRegistry(tmp_path/'agents.sqlite3')
    first=RunController(store,agents)
    try:
        with pytest.raises(RuntimeError,match='already_in_use'):RunController(store,agents)
    finally:first.close()
    for i in range(102):store.create_run({'run_id':f'orphan_{i:03}','state':'RUNNING','nodes':{}})
    reopened=RunController(store,agents)
    try:assert all(store.get(f'orphan_{i:03}')['state']=='FAILED' for i in range(102))
    finally:reopened.close()


def test_indirect_recursive_definition(prepared):
    from app.workflow_compile import compile_workflow
    from app.workflow_models import WorkflowSpec
    from app.registry import RegistryError
    from app.agents import AgentCreate,AgentRecord
    from datetime import datetime,timezone
    a=copy.deepcopy(templates()[5]);a['nodes'][0]['workflow']={'workflow_id':'b','version':1}
    b=copy.deepcopy(a);b['nodes'][0]['workflow']={'workflow_id':'a','version':1}
    resolved={id_:WorkflowSpec.model_validate({k:v for k,v in value.items() if k!='workflow_id'}).model_dump(mode='json') for id_,value in [('a',a),('b',b)]}
    with pytest.raises(RegistryError) as error:
        compile_workflow(WorkflowSpec.model_validate({k:v for k,v in a.items() if k!='workflow_id'}),'a',1,
            lambda id_,version: resolved[id_],prepared.app.state.registry.list())
    assert error.value.code=='recursive_subworkflow'


def test_state_machine_rejects_terminal_rewrite(tmp_path):
    from app.event_store import EventStore
    store=EventStore(tmp_path/'state.sqlite3')
    store.create_run({'run_id':'r','state':'PENDING','nodes':{}})
    store.update('r','started',state='RUNNING');store.update('r','done',state='SUCCEEDED')
    with pytest.raises(RuntimeError,match='illegal_state_transition'):store.update('r','bad',state='RUNNING')


@pytest.mark.parametrize('op,right,expected',[('eq',2.0,True),('lt',3.0,True),('gt',3.0,False),('exists',None,True)])
def test_typed_predicates(client,op,right,expected):
    from app.workflow_models import digest
    definition={'workflow_id':'predicate','input_schema':{'x':{'type':'number'}},'nodes':[
        {'id':'condition','kind':'condition','predicate':{'left':inp('x'),'op':op,'right':right}}],
        'outputs':{'decision':out('condition','result')}}
    assert client.post('/api/workflows',json=definition).status_code==201
    result=finish(client,start(client,'predicate',inputs={'x':2.0}))
    assert result['state']=='SUCCEEDED' and result['nodes']['condition']['result']['result']==expected
    assert result['nodes']['condition']['input_digest']==digest({'left':2.0,'right':right})


def test_sensitive_child_and_definition_limits(prepared):
    child=copy.deepcopy(templates()[3]);child['workflow_id']='private_child';child['input_schema']['numbers']['sensitive']=True
    assert prepared.post('/api/workflows',json=child).status_code==201
    parent=copy.deepcopy(templates()[5]);parent['workflow_id']='private_parent';parent['nodes'][0]['workflow']['workflow_id']='private_child'
    assert prepared.post('/api/workflows',json=parent).json()['error']['code']=='sensitive_child_requires_parent_annotation'
    parent['input_schema']['numbers']['sensitive']=True
    assert prepared.post('/api/workflows',json=parent).status_code==201
    result=finish(prepared,start(prepared,'private_parent'))
    assert result['state']=='SUCCEEDED' and result['result']=='[REDACTED]'
    assert all(n.get('result')=='[REDACTED]' for n in result['nodes'].values() if n['state']=='SUCCEEDED')
    oversized=copy.deepcopy(templates()[3]);oversized['workflow_id']='oversized';oversized['nodes']=[stat('n'+str(i)) for i in range(33)];oversized['outputs']={}
    assert prepared.post('/api/workflows',json=oversized).status_code==422
