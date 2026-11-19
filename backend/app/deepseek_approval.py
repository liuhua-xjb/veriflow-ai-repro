"""Ephemeral process approval. No credential is returned by any HTTP endpoint."""
import hashlib,hmac,os,re,secrets,time
from contextlib import contextmanager
from contextvars import ContextVar
from starlette.responses import JSONResponse
from .deepseek_authorization import EXPIRES
from .llm import ProviderError

HEADER='x-veriflow-trial-approval'
HOSTS={'127.0.0.1:18000','localhost:18000'}
ORIGINS={'http://127.0.0.1:15173','http://localhost:15173'}
_current=ContextVar('veriflow_active_approval',default=None)

class ApprovalSession:
    def __init__(self,token,*,clock=time.monotonic,wall=time.time):
        if not re.fullmatch('[0-9a-f]{64}',token):raise ValueError('INVALID_APPROVAL_FORMAT')
        self._hash=hashlib.sha256(token.encode('ascii')).digest()
        self._pid=os.getpid();self._clock=clock;self._wall=wall
        self._deadline=clock()+1800;self._active=True;self.protected_runs=set()
    @classmethod
    def issue(cls,**kwargs):
        token=secrets.token_hex(32)
        return cls(token,**kwargs),token
    def check(self):
        if not self._active or os.getpid()!=self._pid or self._clock()>=self._deadline or self._wall()>=EXPIRES:
            raise ProviderError('TRIAL_APPROVAL_EXPIRED')
    def authenticate(self,token):
        self.check()
        if not isinstance(token,str) or not re.fullmatch('[0-9a-f]{64}',token):raise ProviderError('TRIAL_APPROVAL_REQUIRED')
        if not hmac.compare_digest(self._hash,hashlib.sha256(token.encode('ascii')).digest()):raise ProviderError('TRIAL_APPROVAL_REQUIRED')
    def close(self):
        self._active=False;self._hash=b'';self.protected_runs.clear()
    @contextmanager
    def action(self,action):
        self.check();grant=ApprovalGrant(self,action);handle=_current.set(grant)
        try:yield grant
        finally:grant.active=False;_current.reset(handle)

class ApprovalGrant:
    def __init__(self,session,action):
        self.session=session;self.action=action;self.run_id=None;self.sent=set();self.active=True
    def check(self):
        self.session.check()
        if not self.active:raise ProviderError('TRIAL_APPROVAL_REQUIRED')
    def bind(self,run_id):
        self.check()
        if self.action!='start' or self.run_id is not None:raise ProviderError('TRIAL_APPROVAL_ALREADY_USED')
        self.run_id=run_id;self.session.protected_runs.add(run_id)

def require_action(action):
    grant=_current.get()
    if not isinstance(grant,ApprovalGrant) or grant.action!=action:raise ProviderError('TRIAL_APPROVAL_REQUIRED')
    grant.check();return grant

def execution_grant(run_id,role,*,consume=False):
    grant=require_action('start')
    if grant.run_id!=run_id or role not in {'planner','reviewer'} or role in grant.sent:raise ProviderError('TRIAL_APPROVAL_SCOPE_MISMATCH')
    if consume:grant.sent.add(role)
    return grant

def current_grant():return _current.get()

class TrialApprovalMiddleware:
    """Authenticate before body parsing; cover the sibling scientific cancel route."""
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope['method']!='POST':return await self.app(scope,receive,send)
        state=scope['app'].state;path=scope['path'].rstrip('/')
        session=getattr(state,'trial_approval',None)
        direct=path.startswith('/api/deepseek/trial/')
        sibling=path.startswith('/api/runs/') and path.endswith('/cancel') and session and path.split('/')[-2] in session.protected_runs
        if not direct and not sibling:return await self.app(scope,receive,send)
        if not session and not hasattr(state,'real_trial'):
            # Preserve the original disabled-service 409 contract; no service can run.
            response=JSONResponse({'error':{'code':'LOCAL_AUTHORIZED_START_REQUIRED'}},409)
            return await response(scope,receive,send)
        headers={}
        for name,value in scope['headers']:headers.setdefault(name.lower(),[]).append(value.decode('latin-1'))
        try:
            if len(headers.get(b'host',[]))!=1 or headers[b'host'][0] not in HOSTS:raise ProviderError('TRIAL_SOURCE_REJECTED')
            origins=headers.get(b'origin',[])
            if len(origins)>1 or (origins and origins[0] not in ORIGINS) or scope.get('query_string'):raise ProviderError('TRIAL_SOURCE_REJECTED')
            values=headers.get(HEADER.encode(),[])
            if not session or len(values)!=1:raise ProviderError('TRIAL_APPROVAL_REQUIRED')
            session.authenticate(values[0])
        except ProviderError as error:
            response=JSONResponse({'error':{'code':error.code}},403 if error.code in {'TRIAL_SOURCE_REJECTED','TRIAL_APPROVAL_EXPIRED'} else 401)
            return await response(scope,receive,send)
        action='cancel' if path.endswith('/cancel') else 'start'
        try:
            with session.action(action):await self.app(scope,receive,send)
        except ProviderError as error:
            # The session can expire between authenticate() and action entry.
            response=JSONResponse({'error':{'code':error.code}},403)
            return await response(scope,receive,send)
