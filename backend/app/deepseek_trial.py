"""Authorized parent run created before Planner. Scientific child remains G3-verified."""
import asyncio,json,os,time,threading
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter,Request
from pydantic import Field
from .contracts import StrictModel
from .deepseek_authorization import ROOT,RealBudgetLedger,authorization
from .deepseek_real_provider import RealDeepSeekProvider,request_body,input_bound,startup_guard
from .deepseek_contracts import PlanProposal,compile_proposal
from .deepseek_preflight import build_review_summary
from .workflow_models import RunCreate,digest
from .event_store import utc
from .registry import RegistryError
from .llm import ProviderError
from .deepseek_approval import require_action,current_grant

router=APIRouter()

class TrialRequest(StrictModel):
    intent: str=Field(default='比较三种模型',max_length=30)

def service(state):
    if not hasattr(state,'real_trial'):
        raise ProviderError('LOCAL_AUTHORIZED_START_REQUIRED')
    return state.real_trial

class TrialService:
    def __init__(self,state,ledger,provider):
        self.state=state;self.ledger=ledger;self.provider=provider;self.lock=threading.Lock();self.tasks={}
    async def start(self,intent):
        grant=require_action('start') if self.provider.mode=='REAL_LLM' else current_grant()
        intents={'比较三种模型':'balanced_comparison','快速基线检查':'latency','比较模型与故障召回':'recall_quality'}
        if intent not in intents:raise ProviderError('UNSAFE_INTENT')
        if not self.lock.acquire(blocking=False):raise ProviderError('TRIAL_ALREADY_RUNNING')
        run_id=None
        try:
            self.ledger.check()
            summary={'intent':intents[intent]};bound=input_bound(request_body('planner',summary))
            self.ledger.can_fit([bound,4000])
            self.provider.prepare()  # Presence only; load the header secret after the atomic reservation.
            from .agents import AgentCreate
            for role in ('planner','reviewer'):
                id_=('ds_real_' if self.provider.mode=='REAL_LLM' else 'ds_trial_sim_')+role+'_v1'
                spec=AgentCreate(id=id_,name='DeepSeek authorized '+role if self.provider.mode=='REAL_LLM' else 'SIMULATED '+role,
                    role='planner' if role=='planner' else 'validator',capabilities=['planning'] if role=='planner' else ['evidence_review'],
                    allowed_tools=[],model_profile='default' if self.provider.mode=='REAL_LLM' else 'offline')
                try:
                    previous=self.state.registry.get(id_)
                    if previous.version!=1 or any(previous.model_dump()[k]!=v for k,v in spec.model_dump(exclude={'id','version'}).items()):raise ProviderError('ROLE_NAMESPACE_CONFLICT')
                except RegistryError as error:
                    if error.status!=404:raise
                    self.state.registry.create(spec)
            run_id='run_'+uuid4().hex
            if grant:grant.bind(run_id)
            self.ledger.reserve_cycle(run_id,bound,4000)
            nodes={name:{'state':'PENDING','kind':'llm_role' if name!='science' else 'subworkflow','attempt_id':name+':1',
                         'dependencies':[] if name=='planner' else ['planner'] if name=='science' else ['science']} for name in ['planner','science','reviewer']}
            self.state.orchestration.create_run({'run_id':run_id,'workflow_id':'deepseek_real_trial','workflow_version':1,'state':'PENDING','nodes':nodes,
                'created_at':utc(),'execution_mode':self.provider.mode+'_COORDINATED','llm_inference':'NOT_EXECUTED','verification_status':'UNVERIFIED'})
            events=self.state.orchestration;audit=self.state.verification
            self.tasks[run_id]=asyncio.current_task()
            events.update(run_id,'TrialStarted',state='RUNNING')
            deadline=time.monotonic()+180.;child=None
            try:
                events.update(run_id,'PlannerStarted','planner','RUNNING')
                plan=await self.provider.complete(run_id,'planner',summary,deadline)
                audit.append(run_id,'LLMRoleResponse',node_id='planner',receipt=plan)
                events.update(run_id,'PlannerResponseObserved',fields={'llm_inference':self.provider.mode+'_PARTIAL'})
                events.update(run_id,'PlannerCompleted','planner','SUCCEEDED',fields={'agent_id':plan['agent_id'],'model':plan['model'],'proposal':plan['proposal']})
                from .science_api import install_demo
                proposal=PlanProposal.model_validate(plan['proposal'])
                if summary['intent']=='balanced_comparison' and set(proposal.models)!={'dummy','logistic','random_forest'}:raise ProviderError('PLAN_DOES_NOT_SATISFY_TASK')
                install_demo(self.state,proposal.seed,'safe',proposal.strategy=='fixed_serial')
                workflow=compile_proposal(proposal,self.state.controller);id_='ds_'+run_id
                self.state.controller.save_workflow(id_,workflow)
                events.update(run_id,'ScienceStarted','science','RUNNING')
                child=self.state.controller.start(RunCreate(workflow_id=id_,workflow_version=1,strategy=proposal.strategy,verification={'mode':proposal.verification}))['run_id']
                if grant:grant.session.protected_runs.add(child)
                events.update(run_id,'ScienceLinked','science',fields={'child_run_id':child})
                while time.monotonic()<deadline:
                    value=events.get(child)
                    if value['state'] in {'SUCCEEDED','FAILED','CANCELLED'}:break
                    await asyncio.sleep(.05)
                else:
                    self.state.controller.cancel(child);raise ProviderError('WORKFLOW_DEADLINE')
                if value['state']!='SUCCEEDED' or value.get('verification_status')!='VERIFIED' or not audit.integrity(child)['ok']:raise ProviderError('SCIENCE_HARD_GATE_FAILED')
                events.update(run_id,'ScienceVerified','science','SUCCEEDED',fields={'verification_status':'VERIFIED'})
                summary=build_review_summary(self.state,child)
                events.update(run_id,'ReviewerStarted','reviewer','RUNNING')
                review=await self.provider.complete(run_id,'reviewer',summary,deadline)
                audit.append(run_id,'LLMRoleResponse',node_id='reviewer',receipt=review,science_run_id=child)
                if review['proposal']['decision']!='accept_evidence':raise ProviderError('REVIEWER_REJECTED_NO_AUTOMATIC_RETRY')
                events.update(run_id,'ReviewerCompleted','reviewer','SUCCEEDED',fields={'agent_id':review['agent_id'],'model':review['model']})
                events.update(run_id,'TrialCompleted',state='SUCCEEDED',fields={'science_run_id':child,'verification_status':'VERIFIED','llm_inference':self.provider.mode,
                    'finished_at':utc(),'budget':self.ledger.snapshot()})
            except (Exception,asyncio.CancelledError) as error:
                code=error.code if isinstance(error,ProviderError) else 'WORKFLOW_FAILED_OR_CANCELLED'
                if child and events.get(child)['state'] not in {'SUCCEEDED','FAILED','CANCELLED'}:self.state.controller.cancel(child)
                current=events.get(run_id)
                for node_id,node in current['nodes'].items():
                    if node['state'] not in {'SUCCEEDED','FAILED','SKIPPED','CANCELLED'}:events.update(run_id,'StageAborted',node_id,'FAILED' if node['state']=='RUNNING' else 'SKIPPED',fields={'error':code})
                events.update(run_id,'TrialFailed',state='FAILED',fields={'error':code,'verification_status':'REJECTED','science_run_id':child,'finished_at':utc(),'budget':self.ledger.snapshot()})
            final=events.get(run_id)
            audit.append(run_id,'RunVerificationFinished',status=final['verification_status'],reason=final.get('error') or 'deterministic_child_verified_and_reviewed',mode=self.provider.mode)
            record={'run':final,'role_receipts':audit.receipts(run_id),'role_integrity':audit.integrity(run_id),'budget':self.ledger.snapshot()}
            directory=ROOT/'reports/G4/deepseek/real'/('runs' if self.provider.mode=='REAL_LLM' else 'simulated');directory.mkdir(parents=True,exist_ok=True)
            with (directory/(run_id+'.json')).open('x',encoding='utf-8') as f:json.dump(record,f,ensure_ascii=False,indent=2)
            if child:
                from .science_api import report_bytes
                for format in ('json','csv','html'):
                    try:raw=report_bytes(self.state,child,format)
                    except RegistryError:continue
                    with (directory/(run_id+'-science.'+format)).open('xb') as f:f.write(raw)
            return record
        finally:
            if run_id:self.tasks.pop(run_id,None)
            self.lock.release()

def attach_real_service(state):
    if os.environ.get('G4_REAL_BACKEND_STARTED')=='1':
        if not getattr(state,'trial_approval',None):raise ProviderError('LOCAL_APPROVAL_BOOTSTRAP_REQUIRED')
        state.trial_approval.check()
        startup_guard()
        ledger=RealBudgetLedger(ROOT/'runtime/deepseek_real_trial.sqlite3')
        state.real_trial=TrialService(state,ledger,RealDeepSeekProvider(ledger))

@router.get('/api/deepseek/trial/status')
def status(request:Request):
    try:
        authorization()
        if not hasattr(request.app.state,'real_trial'):return {'status':'LOCAL_AUTHORIZED_START_REQUIRED','authorization_valid':True,'key_configured':None}
        item=service(request.app.state);snapshot=item.ledger.snapshot()
        try:
            # Status never loads the Key value or returns approval credentials.
            startup_guard();code='READY' if os.environ.get('VERIFLOW_LLM_API_KEY') else 'KEY_NOT_CONFIGURED'
        except ProviderError as error:code=error.code
        session=getattr(request.app.state,'trial_approval',None)
        if session:session.check()
        return {'status':code,'authorization_valid':True,'key_configured':code=='READY','approval_required':True,'budget':snapshot,'active_run_ids':list(item.tasks)}
    except ProviderError as error:return {'status':error.code,'authorization_valid':False,'key_configured':None}

@router.post('/api/deepseek/trial/start')
async def start(value:TrialRequest,request:Request):
    try:return await service(request.app.state).start(value.intent)
    except ProviderError as error:
        status=403 if error.code=='TRIAL_APPROVAL_EXPIRED' else 401 if error.code.startswith('TRIAL_APPROVAL_') else 409
        raise RegistryError(status,error.code)

@router.post('/api/deepseek/trial/{run_id}/cancel')
async def cancel(run_id:str,request:Request):
    try:
        item=service(request.app.state);task=item.tasks.get(run_id)
        if not task:raise RegistryError(409,'TRIAL_NOT_ACTIVE')
        task.cancel();return {'run_id':run_id,'cancel_requested':True}
    except ProviderError as error:raise RegistryError(409,error.code)
