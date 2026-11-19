"""Separate real connector; old preflight refusal and simulated budgets are untouched."""
import asyncio,json,math,os,time
import httpx
from urllib.parse import urlsplit
from .deepseek_authorization import ROOT,EXPIRES,authorization
from .deepseek_contracts import PlanProposal,ReviewProposal,PLANNER_EXAMPLE,REVIEW_EXAMPLE,enforce_review
from .llm import ProviderError
from .workflow_models import digest
from .deepseek_approval import require_action,execution_grant

def request_body(role,summary):
    if role not in {'planner','reviewer'}:raise ProviderError('INVALID_ROLE')
    schema=PlanProposal if role=='planner' else ReviewProposal
    example=PLANNER_EXAMPLE if role=='planner' else REVIEW_EXAMPLE
    if role=='planner':
        if set(summary)!={'intent'} or summary['intent'] not in {'latency','recall_quality','balanced_comparison'}:raise ProviderError('UNSAFE_INTENT')
    else:
        if set(summary)!={'status','evidence_ids','repairs_remaining','metrics'}:raise ProviderError('UNSAFE_EVIDENCE')
        if summary['status'] not in {'VERIFIED','REJECTED','UNVERIFIED'} or summary['repairs_remaining']!=0:raise ProviderError('UNSAFE_EVIDENCE')
        ids=summary['evidence_ids']
        if not isinstance(ids,list) or not 1<=len(ids)<=8 or any(not isinstance(x,str) or len(x)>180 or not x.startswith('run_') for x in ids):raise ProviderError('UNSAFE_EVIDENCE')
        if not isinstance(summary['metrics'],dict) or not set(summary['metrics'])<={'dummy','logistic','random_forest'}:raise ProviderError('UNSAFE_METRICS')
        for values in summary['metrics'].values():
            if not isinstance(values,dict) or not set(values)<={'precision','recall','f1','macro_f1','pr_auc_average_precision','roc_auc'} or any(type(x) not in {int,float} or not math.isfinite(x) or not 0<=x<=1 for x in values.values()):raise ProviderError('UNSAFE_METRICS')
    instruction='You are the '+role+'. Return only json matching schema. No tools, code, paths, new fields or Hard Gate overrides. '
    if role=='planner':instruction+='Choose a permitted model subset and strategy based on intent; use all three models and report_exporter for balanced_comparison. '
    else:instruction+='Copy only provided evidence IDs. Reject if Hard Gate is REJECTED; no retry permission is granted. '
    prompt=instruction+'Example json: '+json.dumps(example)+' Schema: '+json.dumps(schema.model_json_schema())
    return {'model':'deepseek-flash','thinking':{'type':'disabled'},'response_format':{'type':'json_object'},'max_tokens':800,'stream':False,
            'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps(summary,sort_keys=True)}]}

def input_bound(body):return len(json.dumps(body,ensure_ascii=False).encode('utf-8'))+256

def startup_guard():
    authorization()
    if os.environ.get('G4_LLM_REAL_CALLS_AUTHORIZED')!='true' or os.environ.get('G4_REAL_BACKEND_STARTED')!='1':raise ProviderError('LOCAL_AUTHORIZED_START_REQUIRED')
    ready=ROOT/'reports/G4/deepseek/real/READY.json'
    if not ready.is_file():raise ProviderError('SAFETY_CHECKS_NOT_READY')
    import hashlib
    record=json.loads(ready.read_text(encoding='utf-8'))
    if record.get('tests_passed') is not True or not record.get('source_sha256'):raise ProviderError('SAFETY_CHECKS_NOT_READY')
    for path,sha in record['source_sha256'].items():
        if hashlib.sha256((ROOT/path).read_bytes()).hexdigest()!=sha:raise ProviderError('READY_SOURCE_CHANGED')

class RealDeepSeekProvider:
    endpoint='https://api.deepseek.com/chat/completions'
    mode='REAL_LLM'
    def __init__(self,ledger):
        if ledger.mode!='REAL_LLM':raise ProviderError('LEDGER_MODE_MISMATCH')
        self.ledger=ledger
    def get_secret(self):
        require_action('start')
        startup_guard()
        secret=os.environ.get('VERIFLOW_LLM_API_KEY','')
        if not secret.strip() or any(c in secret for c in '\r\n'):raise ProviderError('KEY_NOT_CONFIGURED')
        return secret
    def prepare(self):
        require_action('start')
        startup_guard()
        if not os.environ.get('VERIFLOW_LLM_API_KEY'):raise ProviderError('KEY_NOT_CONFIGURED')
    async def complete(self,run_id,role,summary,workflow_deadline):
        simulated=self.validate_transport()
        if not simulated:
            # Preserve no-Key failure without loading its value or touching a ledger.
            if 'VERIFLOW_LLM_API_KEY' not in os.environ:raise ProviderError('KEY_NOT_CONFIGURED')
            execution_grant(run_id,role)
        body=request_body(role,summary);bound=input_bound(body)
        self.ledger.check()
        if time.monotonic()>=workflow_deadline:raise ProviderError('WORKFLOW_DEADLINE')
        # Inspect the existing reservation before loading the key; dispatch repeats the atomic check.
        with self.ledger.db() as db:
            row=db.execute('SELECT input_bound,status FROM slots WHERE run_id=? AND role=?',(run_id,role)).fetchone()
        if not row or row[1]!='HELD' or bound>row[0]:raise ProviderError('NO_VALID_RESERVATION')
        secret=self.get_secret()
        self.ledger.check()
        self.ledger.dispatch(run_id,role,digest(body),bound)
        return await self.exchange(run_id,role,summary,body,bound,secret,workflow_deadline)
    def validate_transport(self):
        if type(self) is SimulatedAuthorizedProvider:
            parsed=urlsplit(self.endpoint)
            if self.mode!='SIMULATED' or self.ledger.mode!='SIMULATED' or parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.path!='/chat/completions' or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ProviderError('ENDPOINT_REJECTED')
            return True
        if self.mode!='REAL_LLM' or self.ledger.mode!='REAL_LLM' or self.endpoint!='https://api.deepseek.com/chat/completions':raise ProviderError('ENDPOINT_REJECTED')
        return False
    async def exchange(self,run_id,role,summary,body,bound,secret,workflow_deadline):
        if not self.validate_transport():
            execution_grant(run_id,role,consume=True)
            self.ledger.check();startup_guard()
            if body!=request_body(role,summary) or input_bound(body)!=bound:raise ProviderError('REQUEST_SCOPE_REJECTED')
            with self.ledger.db() as db:
                slot=db.execute('SELECT status,request_hash FROM slots WHERE run_id=? AND role=?',(run_id,role)).fetchone()
            if not slot or slot!=('SENT_OR_UNKNOWN',digest(body)):raise ProviderError('NO_VALID_RESERVATION')
            if secret!=self.get_secret():raise ProviderError('REQUEST_CREDENTIAL_SCOPE_REJECTED')
        start=time.monotonic();usage=None;received=False
        try:
            timeout=min(30.,EXPIRES-self.ledger.clock(),workflow_deadline-time.monotonic())
            if timeout<=0:raise ProviderError('AUTHORIZATION_OR_WORKFLOW_EXPIRED')
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=timeout) as client:
                    headers={'Authorization':'Bearer '+secret} if self.mode=='REAL_LLM' else {}
                    async with client.stream('POST',self.endpoint,json=body,headers=headers) as response:
                        received=True
                        if response.status_code!=200:raise ProviderError('HTTP_'+str(response.status_code))
                        raw=bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw)>65536:raise ProviderError('RESPONSE_TOO_LARGE')
            if secret and secret in raw.decode('utf-8','ignore'):raise ProviderError('SECRET_ECHO_REJECTED')
            data=json.loads(raw);u=data['usage'];a,b,t=(u[k] for k in ('prompt_tokens','completion_tokens','total_tokens'))
            if any(type(v) is not int or v<0 for v in (a,b,t)) or a+b!=t or a>bound or b>800:raise ProviderError('USAGE_INVALID')
            usage={'prompt_tokens':a,'completion_tokens':b,'total_tokens':t}
            choice=data['choices'][0];message=choice['message']
            if data['model'] not in {'deepseek-flash','DeepSeek-V4.1-Flash-1017'} or choice['finish_reason']!='stop' or message.get('role')!='assistant' or message.get('tool_calls') or message.get('refusal'):raise ProviderError('MODEL_OR_FINISH_REJECTED')
            content=message.get('content')
            if not isinstance(content,str) or not content.strip():raise ProviderError('EMPTY_CONTENT')
            schema=PlanProposal if role=='planner' else ReviewProposal;proposal=schema.model_validate_json(content)
            if role=='reviewer':enforce_review(proposal,summary['status'],summary['evidence_ids'],0)
            result={'mode':self.mode,'provider':'deepseek' if self.mode=='REAL_LLM' else 'local_simulator','run_id':run_id,'node_id':role,'attempt_id':role+':1',
                    'agent_id':('ds_real_' if self.mode=='REAL_LLM' else 'ds_trial_sim_')+role+'_v1','agent_version':1,'model':data['model'],'prompt_version':'real-'+role+'-v1',
                    'schema_version':proposal.schema_version,'proposal':proposal.model_dump(),'usage':usage,'official_response_received':received and self.mode=='REAL_LLM',
                    'latency_ms':(time.monotonic()-start)*1000,'request_digest':digest(body),'response_digest':digest(proposal.model_dump()),
                    'estimated_usage_usd_peak':(a*.3+b*1.2)/1e6,'supplier_actual_bill_usd':None}
            self.ledger.finish(run_id,role,'COMPLETED',result);return result
        except ProviderError as error:code=error.code
        except (asyncio.CancelledError,TimeoutError,httpx.TimeoutException):code='CANCELLED_OR_TIMEOUT_UNKNOWN_BILLING'
        except (ValueError,TypeError,KeyError,IndexError,RecursionError,httpx.HTTPError):code='RESPONSE_OR_NETWORK_INVALID'
        self.ledger.finish(run_id,role,'FAILED',{'error':code,'usage':usage,'official_response_received':received and self.mode=='REAL_LLM',
            'latency_ms':(time.monotonic()-start)*1000,'supplier_actual_bill_usd':None,
            'estimated_usage_usd_peak':(usage['prompt_tokens']*.3+usage['completion_tokens']*1.2)/1e6 if usage else None})
        raise ProviderError(code)

class SimulatedAuthorizedProvider(RealDeepSeekProvider):
    mode='SIMULATED'
    def __init__(self,ledger,endpoint):
        parsed=urlsplit(endpoint)
        if ledger.mode!='SIMULATED' or parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('SIMULATOR_ONLY')
        self.ledger=ledger;self.endpoint=endpoint+'/chat/completions'
    def get_secret(self):return ''
    def prepare(self):return None
