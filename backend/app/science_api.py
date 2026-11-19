"""Local offline demo and immutable report downloads. REAL_LLM remains gated."""
import csv,html,io,json
from pathlib import Path
from typing import Literal
from fastapi import APIRouter,Request
from fastapi.responses import Response
from pydantic import Field
from .contracts import StrictModel
from .registry import RegistryError
from .science_contracts import FEATURES,DATASET_SHA
from .science_data import source,load_data,profile
from .workflow_models import RunCreate,WorkflowSpec,digest
from .agents import AgentCreate

router=APIRouter()


class DemoRequest(StrictModel):
    seed: Literal[42,43,44,45,46] = 42
    strategy: Literal['fixed_serial','dynamic'] = 'dynamic'
    verification: Literal['execution_only','verify_only','verify_and_repair'] = 'verify_and_repair'
    feature_set: Literal['safe','leakage_negative'] = 'safe'
    reasoning: Literal['offline','real_llm'] = 'offline'
    idempotency_key: str | None = Field(default=None,min_length=1,max_length=100)


def literal(value):return {'source':'literal','value':value}
def previous(node):return {'source':'node','node_id':node,'field':'artifact_id'}


def definition(seed=42,feature_set='safe',single=False):
    features=FEATURES if feature_set=='safe' else FEATURES+['TWF']
    def node(id_,tool,inputs,capability):
        return {'id':id_,'kind':'tool_task','tool':tool,'capability':capability,'timeout_seconds':10.,'inputs':inputs,
                **({'agent_id':'g4_offline_single'} if single else {})}
    nodes=[node('profile','data_profiler',{'dataset':literal('ai4i2020')},'data_science'),
        node('leakage','leakage_check',{'profile_id':previous('profile'),'features':literal(features)},'data_science')]
    edges=[{'source':'profile','target':'leakage'}]
    for model in ('dummy','logistic','random_forest'):
        nodes += [node('train_'+model,'model_trainer',{'leakage_id':previous('leakage'),'model':literal(model),'seed':literal(seed)},'model_training'),
                  node('eval_'+model,'model_evaluator',{'training_id':previous('train_'+model)},'model_evaluation')]
        edges += [{'source':'leakage','target':'train_'+model},{'source':'train_'+model,'target':'eval_'+model}]
    nodes.append(node('export','report_exporter',{'dummy_id':previous('eval_dummy'),'logistic_id':previous('eval_logistic'),'forest_id':previous('eval_random_forest')},'reporting'))
    edges += [{'source':'eval_'+model,'target':'export'} for model in ('dummy','logistic','random_forest')]
    return {'description':f'G4_OFFLINE UCI synthetic ML; seed={seed}; features={feature_set}; single={single}; no LLM',
            'nodes':nodes,'edges':edges,'outputs':{'report':previous('export')},'timeout_seconds':30.,'max_concurrency':4}


def install_demo(state,seed,feature_set,single):
    # Fixed namespace: refuse to revise any pre-existing incompatible user data.
    all_tools=['data_profiler','leakage_check','model_trainer','model_evaluator','report_exporter']
    specs=[{'id':'g4_offline_single','name':'G4 OFFLINE single baseline','role':'analyst','capabilities':['data_science','model_training','model_evaluation','reporting'],'allowed_tools':all_tools,'concurrency_limit':1},
        {'id':'g4_data','name':'G4 OFFLINE data checks','role':'analyst','capabilities':['data_science'],'allowed_tools':['data_profiler','leakage_check'],'concurrency_limit':1},
        {'id':'g4_train_a','name':'G4 OFFLINE trainer A','role':'analyst','capabilities':['model_training'],'allowed_tools':['model_trainer'],'concurrency_limit':1},
        {'id':'g4_train_b','name':'G4 OFFLINE trainer B','role':'analyst','capabilities':['model_training'],'allowed_tools':['model_trainer'],'concurrency_limit':1},
        {'id':'g4_evaluator','name':'G4 OFFLINE evaluation','role':'validator','capabilities':['model_evaluation','reporting'],'allowed_tools':['model_evaluator','report_exporter'],'concurrency_limit':2}]
    for spec in specs:
        value=AgentCreate.model_validate(spec)
        try:
            old=state.registry.get(value.id)
            if any(old.model_dump()[k]!=value.model_dump()[k] for k in spec if k!='id'):raise RegistryError(409,'g4_agent_namespace_conflict')
        except RegistryError as error:
            if error.status!=404:raise
            state.registry.create(value)
    id_=f'g4_offline_{seed}_{feature_set}_'+('single' if single else 'multi')
    spec=WorkflowSpec.model_validate(definition(seed,feature_set,single))
    try:
        old=state.orchestration.workflow(id_,1)
        if any(old[k]!=v for k,v in spec.model_dump(mode='json').items()):raise RegistryError(409,'g4_workflow_namespace_conflict')
    except RegistryError as error:
        if error.status!=404:raise
        state.controller.save_workflow(id_,spec)
    return id_


@router.get('/api/science/source')
def dataset_source():
    try:return {'source':source(),'profile':profile(load_data()),'mode':'G4_OFFLINE','llm_status':'BLOCKED_NOT_AUTHORIZED'}
    except (OSError,ValueError):raise RegistryError(503,'official_dataset_unavailable_or_changed')


@router.get('/api/science/tools')
def science_tools(request:Request):return request.app.state.controller.catalog.discover()[2:]


@router.get('/api/science/llm')
def llm_status():
    return {'status':'BLOCKED','authorization':False,'calls':0,'tokens':0,'cost':0,
            'roles':{'Planner':'NOT_EXECUTED','Reviewer':'NOT_EXECUTED'},
            'reason':'No explicit model/endpoint/license/compute or API cost budget authorization. G4-A06 BLOCKED.'}


@router.post('/api/science/demos',status_code=202)
def demo(value:DemoRequest,request:Request):
    if value.reasoning=='real_llm':raise RegistryError(409,'real_llm_resource_authorization_required')
    state=request.app.state;single=value.strategy=='fixed_serial'
    # Install against latest Agent/workflow registry under the existing controller lock.
    with state.controller.lock:
        id_=install_demo(state,value.seed,value.feature_set,single)
        return state.controller.start(RunCreate(workflow_id=id_,workflow_version=1,strategy=value.strategy,
            verification={'mode':value.verification},idempotency_key=value.idempotency_key))


@router.get('/api/science/runs/{run_id}')
def science_run(run_id:str,request:Request):
    state=request.app.state;run=state.orchestration.get(run_id)
    try:records=state.controller.catalog.artifacts.list(run_id)
    except (OSError,ValueError,KeyError):raise RegistryError(409,'artifact_integrity_failed')
    summaries=[]
    for record in records:
        payload={k:v for k,v in record['payload'].items() if k not in {'train_indices','test_indices','labels','predictions','probabilities'}}
        summaries.append({**{k:v for k,v in record.items() if k!='payload'},'payload':payload})
    return {'run':run,'artifacts':summaries,'source':source(),'mode':'G4_OFFLINE','llm':llm_status(),
            'downloads':{kind:f'/api/science/runs/{run_id}/report/{kind}' for kind in ('json','csv','html')} if run['state']=='SUCCEEDED' and any(r['tool']=='report_exporter' for r in records) else {}}


def report_bytes(state,run_id,format):
    run=state.orchestration.get(run_id)
    if run['state']!='SUCCEEDED':raise RegistryError(409,'successful_export_not_available')
    records=state.controller.catalog.artifacts.list(run_id)
    exported=[r for r in records if r['tool']=='report_exporter']
    if not exported:raise RegistryError(404,'report_not_found')
    data={'run':run,'report':exported[-1],'artifacts':[{k:v for k,v in r.items() if k!='payload'} for r in records],
          'verification':state.verification.receipts(run_id),'integrity':state.verification.integrity(run_id)}
    if not data['integrity']['ok']:raise RegistryError(409,'audit_integrity_failed')
    if format=='json':raw=json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')
    elif format=='csv':
        text=io.StringIO(newline='');writer=csv.writer(text);writer.writerow(['run_id','data_sha256','mode','model','seed','metric','value'])
        for model in exported[-1]['payload']['models']:
            for key,value in model['metrics'].items():writer.writerow([run_id,DATASET_SHA,'G4_OFFLINE',model['model'],model['seed'],key,json.dumps(value)])
        raw=text.getvalue().encode('utf-8-sig')
    else:raw=('<!doctype html><meta charset="utf-8"><title>VeriFlow G4 OFFLINE</title><h1>UCI AI4I 2020 合成数据报告</h1><p>G4_OFFLINE · REAL_LLM BLOCKED · 不代表真实工厂验证</p><pre>'+html.escape(json.dumps(data,ensure_ascii=False,indent=2))+'</pre>').encode('utf-8')
    path=state.controller.catalog.artifacts.directory(run_id)/('report.'+format)
    if path.is_symlink():raise RegistryError(409,'report_path_rejected')
    if not path.exists():
        try:
            with path.open('xb') as f:f.write(raw)
        except FileExistsError:pass
    if path.read_bytes()!=raw:raise RegistryError(409,'report_integrity_failed')
    return raw


@router.get('/api/science/runs/{run_id}/report/{format}')
def download(run_id:str,format:Literal['json','csv','html'],request:Request):
    raw=report_bytes(request.app.state,run_id,format)
    return Response(raw,media_type={'json':'application/json','csv':'text/csv','html':'text/html'}[format],
                    headers={'Content-Disposition':f'attachment; filename="veriflow-{run_id}.{format}"','X-Content-Type-Options':'nosniff'})
