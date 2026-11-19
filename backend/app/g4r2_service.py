"""Single explicitly approved unit per request, no batch auto-loop or retry."""
import asyncio,json,threading,time
from uuid import uuid4
from .g4r2_policy import OUT,ID,unit
from .g4r2_approval import require_r2
from .deepseek_contracts import PlanProposal,compile_proposal
from .workflow_models import RunCreate
from .event_store import utc
from .llm import ProviderError
from .agents import AgentCreate
from .registry import RegistryError

class G4R2Service:
    def __init__(self,state,ledger,provider):self.state=state;self.ledger=ledger;self.provider=provider;self.lock=threading.Lock();self.tasks={}
    async def start(self,key):
        grant=require_r2('start');self.provider.prepare()  # Phase/review before ledger, agents, run or Key load.
        u=unit(key)
        if not self.lock.acquire(False):raise ProviderError('G4R2_BATCH_BUSY')
        run_id=None;child=None;events=self.state.orchestration;audit=self.state.verification
        try:
            run_id='run_g4r2_'+uuid4().hex
            self.ledger.reserve(key,run_id);grant.bind(run_id)
            roles=['planner','science','reviewer'] if key.startswith('main:') else ['planner']
            nodes={r:{'state':'PENDING','kind':'llm_role' if r!='science' else 'subworkflow','dependencies':roles[:roles.index(r)],'attempt_id':r+':1'} for r in roles}
            events.create_run({'run_id':run_id,'workflow_id':'g4r2_main' if len(roles)==3 else 'g4r2_planner_only','workflow_version':1,'state':'PENDING',
                'nodes':nodes,'created_at':utc(),'authorization_id':ID,'unit_key':key,'seed':u.seed,'intent':u.intent,
                'execution_mode':self.provider.mode+'_G4R2','llm_inference':'NOT_CONFIRMED','verification_status':'UNVERIFIED'})
            self.tasks[run_id]=asyncio.current_task();events.update(run_id,'G4R2Started',state='RUNNING')
            deadline=time.monotonic()+180
            try:
                for role in ('planner','reviewer') if len(roles)==3 else ('planner',):
                    id_=('g4r2_real_' if self.provider.mode=='REAL_LLM' else 'g4r2_sim_')+role+'_v1'
                    spec=AgentCreate(id=id_,name='G4R2 '+self.provider.mode+' '+role,role='planner' if role=='planner' else 'validator',
                        capabilities=['planning'] if role=='planner' else ['evidence_review'],allowed_tools=[],model_profile='offline')
                    try:
                        old=self.state.registry.get(id_)
                        if old.version!=1 or any(old.model_dump()[k]!=v for k,v in spec.model_dump(exclude={'id'}).items()):raise ProviderError('G4R2_AGENT_NAMESPACE_CONFLICT')
                    except RegistryError as error:
                        if error.status!=404:raise
                        self.state.registry.create(spec)
                events.update(run_id,'PlannerStarted','planner','RUNNING')
                plan=await self.provider.complete(run_id,'planner',deadline)
                audit.append(run_id,'G4R2RoleResponse',receipt=plan)
                events.update(run_id,'PlannerCompleted','planner','SUCCEEDED',fields={'agent_id':plan['agent_id'],'proposal':plan['proposal']})
                events.update(run_id,'PlannerObserved',fields={'llm_inference':self.provider.mode+'_PARTIAL'})
                if len(roles)==3:
                    from .science_api import install_demo
                    p=PlanProposal.model_validate(plan['proposal']);install_demo(self.state,u.seed,'safe',p.strategy=='fixed_serial')
                    spec=compile_proposal(p,self.state.controller);workflow_id='g4r2_'+run_id
                    self.state.controller.save_workflow(workflow_id,spec)
                    events.update(run_id,'ScienceStarted','science','RUNNING')
                    child=self.state.controller.start(RunCreate(workflow_id=workflow_id,workflow_version=1,strategy=p.strategy,verification={'mode':p.verification}))['run_id']
                    grant.session.protected_runs.add(child)
                    events.update(run_id,'ScienceLinked',fields={'science_run_id':child})
                    while time.monotonic()<deadline:
                        value=events.get(child)
                        if value['state'] in {'SUCCEEDED','FAILED','CANCELLED'}:break
                        await asyncio.sleep(.05)
                    else:raise ProviderError('G4R2_WORKFLOW_DEADLINE')
                    self.provider.bind_review(run_id,child)
                    events.update(run_id,'ScienceVerified','science','SUCCEEDED',fields={'verification_status':'VERIFIED'})
                    events.update(run_id,'ReviewerStarted','reviewer','RUNNING')
                    review=await self.provider.complete(run_id,'reviewer',deadline)
                    audit.append(run_id,'G4R2RoleResponse',receipt=review,science_run_id=child)
                    if review['proposal']['decision']!='accept_evidence':raise ProviderError('G4R2_REVIEW_REJECTED')
                    events.update(run_id,'ReviewerCompleted','reviewer','SUCCEEDED',fields={'agent_id':review['agent_id']})
                self.ledger.finish(run_id,'SUCCEEDED')
                events.update(run_id,'G4R2Completed',state='SUCCEEDED',fields={'verification_status':'VERIFIED' if child else 'PLANNER_ONLY_NOT_SCIENCE_VERIFIED',
                    'llm_inference':self.provider.mode if child else self.provider.mode+'_PLANNER_ONLY','finished_at':utc()})
            except (Exception,asyncio.CancelledError) as error:
                code=error.code if isinstance(error,ProviderError) else 'G4R2_FAILED_OR_CANCELLED'
                if child and events.get(child)['state'] not in {'SUCCEEDED','FAILED','CANCELLED'}:self.state.controller.cancel(child)
                for name,n in events.get(run_id)['nodes'].items():
                    if n['state'] not in {'SUCCEEDED','FAILED','SKIPPED','CANCELLED'}:events.update(run_id,'StageAborted',name,'FAILED' if n['state']=='RUNNING' else 'SKIPPED',fields={'error':code})
                self.ledger.finish(run_id,'FAILED',code)
                events.update(run_id,'G4R2Failed',state='FAILED',fields={'verification_status':'REJECTED','error':code,'finished_at':utc()})
            final=events.get(run_id);audit.append(run_id,'RunVerificationFinished',status=final['verification_status'],mode=self.provider.mode)
            record={'mode':self.provider.mode,'test_label':'TEST_ONLY' if self.provider.mode=='SIMULATED' else None,'run':final,
                'role_receipts':audit.receipts(run_id),'integrity':audit.integrity(run_id),'budget':self.ledger.snapshot()}
            if child:
                record['science']={'run':events.get(child),'events':events.events(child,0,100),'verification':audit.receipts(child),
                    'integrity':audit.integrity(child),'artifacts':self.state.controller.catalog.artifacts.list(child)}
            directory=OUT/('real-runs' if self.provider.mode=='REAL_LLM' else 'simulated');directory.mkdir(parents=True,exist_ok=True)
            with (directory/(run_id+'.json')).open('x',encoding='utf-8') as f:json.dump(record,f,ensure_ascii=False,indent=2)
            if child:
                from .science_api import report_bytes
                for fmt in ('json','csv','html'):
                    try:data=report_bytes(self.state,child,fmt)
                    except RegistryError:continue
                    with (directory/(run_id+'-science.'+fmt)).open('xb') as f:f.write(data)
            return record
        finally:
            if run_id:self.tasks.pop(run_id,None);self.provider.reviews.pop(run_id,None)
            self.lock.release()
    def cancel(self,run_id):
        require_r2('cancel')
        if run_id not in self.tasks:raise ProviderError('G4R2_RUN_NOT_ACTIVE')
        self.tasks[run_id].cancel();return {'run_id':run_id,'cancel_requested':True}
