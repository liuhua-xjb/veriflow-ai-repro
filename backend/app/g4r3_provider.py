"""R3 fixed-URL transport, unchanged repeat prompts and no-fallback receipts."""
import asyncio,json,os,time
from urllib.parse import urlsplit
import httpx
from .g4r3_policy import ROOT,ID,ENDPOINT,EXPIRES,phase_gate,unit,bounds
from .g4r3_ledger import G4R3Ledger
from .g4r3_approval import require_r3,r3_execution
from .deepseek_repeat_prep import repeat_body,validate_plan,validate_review,PROMPT_VERSIONS,BINDING_VERSION
from .deepseek_real_provider import input_bound
from .workflow_models import digest
from .llm import ProviderError
PRODUCTION_LEDGER=ROOT/'runtime/deepseek_g4r3.sqlite3'

class G4R3Provider:
    mode='REAL_LLM';endpoint=ENDPOINT
    def __init__(self,ledger,state):
        if type(ledger)is not G4R3Ledger or ledger.path!=PRODUCTION_LEDGER or ledger.mode!='REAL_LLM':raise ProviderError('G4R3_PRODUCTION_LEDGER_REQUIRED')
        ledger.assert_bound('REAL_LLM')
        self.ledger=ledger;self.state=state;self.reviews={}
    def transport_guard(self):
        if type(self)is G4R3SimulatedProvider:
            p=urlsplit(self.endpoint)
            if self.mode!='SIMULATED' or self.ledger.mode!='SIMULATED' or p.scheme!='http' or p.hostname!='127.0.0.1' or not p.port or p.path!='/chat/completions' or p.username or p.password or p.query or p.fragment:raise ProviderError('G4R3_ENDPOINT_REJECTED')
            return True
        if type(self)is not G4R3Provider or self.mode!='REAL_LLM' or self.ledger.mode!='REAL_LLM' or self.ledger.path!=PRODUCTION_LEDGER or self.endpoint!=ENDPOINT:raise ProviderError('G4R3_ENDPOINT_REJECTED')
        return False
    def prepare(self):
        require_r3('start');self.transport_guard()
        if self.mode=='REAL_LLM':
            phase_gate()  # Must fail in Phase P before even checking Key presence.
            self.ledger.assert_bound('REAL_LLM')
            if 'VERIFLOW_G4R3_API_KEY' not in os.environ:raise ProviderError('KEY_NOT_CONFIGURED')
    def secret(self):
        self.prepare()
        if self.mode=='SIMULATED':return ''
        value=os.environ.get('VERIFLOW_G4R3_API_KEY','')
        if not value.strip() or '\n' in value or '\r' in value:raise ProviderError('KEY_NOT_CONFIGURED')
        return value
    def bind_review(self,run_id,child):
        r3_execution(run_id,'reviewer')
        from .g4r3_science import science_summary
        key=self.ledger.context(run_id)
        if not key.startswith('main:'):raise ProviderError('G4R3_NO_REVIEW_FOR_EXTRA')
        self.reviews[run_id]=(child,science_summary(self.state,run_id,child,unit(key)))
    def body(self,run_id,role):
        key=self.ledger.context(run_id);u=unit(key);summary=None
        if role=='reviewer':
            if run_id not in self.reviews:raise ProviderError('G4R3_NO_TRUSTED_REVIEW')
            child,saved=self.reviews[run_id]
            from .g4r3_science import science_summary
            summary=science_summary(self.state,run_id,child,u)
            if summary!=saved:raise ProviderError('G4R3_SCIENCE_EVIDENCE_CHANGED')
        return key,u,summary,repeat_body(u,role,summary)
    async def complete(self,run_id,role,deadline):
        self.prepare();r3_execution(run_id,role);self.ledger.check()
        if time.monotonic()>=deadline:raise ProviderError('G4R3_WORKFLOW_DEADLINE')
        key,u,summary,body=self.body(run_id,role);bound=input_bound(body)
        slot=self.ledger.slot(run_id,role)
        if slot[1]!='HELD' or bound>slot[0] or bound>4000:raise ProviderError('G4R3_INPUT_RESERVATION_EXCEEDED')
        secret=self.secret();self.ledger.dispatch(run_id,role,digest(body),bound)
        return await self.exchange(run_id,role,deadline,body,secret)
    async def exchange(self,run_id,role,deadline,body,secret):
        self.prepare();grant=r3_execution(run_id,role,consume=True);self.ledger.check()
        key,u,summary,expected=self.body(run_id,role);slot=self.ledger.slot(run_id,role)
        if body!=expected or slot[1:]!=('SENT_OR_UNKNOWN',digest(body)) or input_bound(body)>slot[0]:raise ProviderError('G4R3_DISPATCH_SCOPE_MISMATCH')
        if secret!=self.secret():raise ProviderError('G4R3_SECRET_SCOPE_MISMATCH')
        started=time.monotonic();usage=None;received=False;code=None
        try:
            timeout=min(30.,EXPIRES-self.ledger.clock(),deadline-time.monotonic(),grant.session.remaining())
            if timeout<=0:raise ProviderError('G4R3_DEADLINE_EXPIRED')
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
            data=json.loads(raw);v=data['usage'];a,b,t=(v[k] for k in ('prompt_tokens','completion_tokens','total_tokens'))
            if any(type(x)is not int or x<0 for x in (a,b,t)) or a+b!=t or a>slot[0] or b>800:raise ProviderError('USAGE_INVALID')
            usage={'prompt_tokens':a,'completion_tokens':b,'total_tokens':t}
            choice=data['choices'][0];message=choice['message']
            if data['model'] not in {'deepseek-flash','DeepSeek-V4.1-Flash-1017'} or choice['finish_reason']!='stop' or message.get('role')!='assistant' or message.get('tool_calls') or message.get('refusal'):raise ProviderError('MODEL_OR_FINISH_REJECTED')
            content=message.get('content')
            if not isinstance(content,str) or not content.strip():raise ProviderError('EMPTY_CONTENT')
            parsed=validate_plan(content,u) if role=='planner' else validate_review(content,u,summary)
            result={'mode':self.mode,'test_label':'TEST_ONLY' if self.mode=='SIMULATED' else None,
                'authorization_id':ID,'run_id':run_id,'node_id':role,'attempt_id':role+':1','unit_key':key,'seed':u.seed,
                'agent_id':('g4r3_real_' if self.mode=='REAL_LLM' else 'g4r3_sim_')+role+'_v1','agent_version':1,
                'provider':'deepseek' if self.mode=='REAL_LLM' else 'loopback_simulator','model':data['model'],'finish_reason':choice['finish_reason'],
                'prompt_version':PROMPT_VERSIONS[role],'binding_version':BINDING_VERSION,'schema_version':parsed.schema_version,
                'proposal':parsed.model_dump(),'usage':usage,'request_digest':digest(body),'response_digest':digest(parsed.model_dump()),
                'latency_ms':(time.monotonic()-started)*1000,'official_model_response_received':self.mode=='REAL_LLM',
                'official_response_received':self.mode=='REAL_LLM','estimated_usage_usd_peak':(a*.30+b*1.20)/1e6,'supplier_actual_bill_usd':None}
            self.ledger.settle(run_id,role,'COMPLETED',result);return result
        except ProviderError as error:code=error.code
        except (asyncio.CancelledError,TimeoutError,httpx.TimeoutException):code='CANCELLED_OR_TIMEOUT_UNKNOWN_BILLING'
        except (ValueError,KeyError,TypeError,IndexError,RecursionError,httpx.HTTPError):code='INVALID_RESPONSE_OR_NETWORK'
        self.ledger.settle(run_id,role,'FAILED',{'error':code,'usage':usage,'http_response_received':received,
            'official_model_response_received':False,'latency_ms':(time.monotonic()-started)*1000,
            'estimated_usage_usd_peak':(usage['prompt_tokens']*.30+usage['completion_tokens']*1.20)/1e6 if usage else None,'supplier_actual_bill_usd':None})
        raise ProviderError(code)

class G4R3SimulatedProvider(G4R3Provider):
    mode='SIMULATED'
    def __init__(self,ledger,state,endpoint):
        p=urlsplit(endpoint)
        if type(ledger)is not G4R3Ledger or ledger.mode!='SIMULATED' or p.scheme!='http' or p.hostname!='127.0.0.1' or not p.port or p.path or p.username or p.password or p.query or p.fragment:raise ProviderError('SIMULATOR_ONLY')
        ledger.assert_bound('SIMULATED')
        self.ledger=ledger;self.state=state;self.endpoint=endpoint+'/chat/completions';self.reviews={}
