"""Independent R3 token and deadline; reuse reviewed action capabilities."""
import os
from .deepseek_approval import ApprovalSession,require_action,execution_grant,HOSTS,ORIGINS
from .g4r3_policy import START,EXPIRES
from .llm import ProviderError
from starlette.responses import JSONResponse
HEADER='x-veriflow-g4r3-approval'
class R3ApprovalSession(ApprovalSession):
    def check(self):
        if not self._active or os.getpid()!=self._pid or self._clock()>=self._deadline:raise ProviderError('G4R3_APPROVAL_EXPIRED')
        if not START<=self._wall()<EXPIRES:raise ProviderError('G4R3_APPROVAL_EXPIRED')
    def remaining(self):self.check();return min(self._deadline-self._clock(),EXPIRES-self._wall())
def require_r3(action):
    grant=require_action(action)
    if type(grant.session)is not R3ApprovalSession:raise ProviderError('G4R3_APPROVAL_REQUIRED')
    return grant
def r3_execution(run_id,role,consume=False):
    require_r3('start');return execution_grant(run_id,role,consume=consume)
class R3ApprovalMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope['method']!='POST':return await self.app(scope,receive,send)
        session=getattr(scope['app'].state,'g4r3_approval',None);path=scope['path'].rstrip('/')
        direct=path.startswith('/api/g4r3/')
        sibling=path.startswith('/api/runs/') and path.endswith('/cancel') and session and path.split('/')[-2] in session.protected_runs
        if not direct and not sibling:return await self.app(scope,receive,send)
        headers={}
        for k,v in scope['headers']:headers.setdefault(k.lower(),[]).append(v.decode('latin-1'))
        try:
            if len(headers.get(b'host',[]))!=1 or headers[b'host'][0] not in HOSTS:raise ProviderError('G4R3_SOURCE_REJECTED')
            origins=headers.get(b'origin',[])
            if len(origins)>1 or origins and origins[0] not in ORIGINS or scope.get('query_string'):raise ProviderError('G4R3_SOURCE_REJECTED')
            tokens=headers.get(HEADER.encode(),[])
            if type(session)is not R3ApprovalSession or len(tokens)!=1:raise ProviderError('G4R3_APPROVAL_REQUIRED')
            session.authenticate(tokens[0])
        except ProviderError as e:
            return await JSONResponse({'error':{'code':e.code}},401 if 'REQUIRED' in e.code else 403)(scope,receive,send)
        try:
            with session.action('cancel' if path.endswith('/cancel') else 'start'):await self.app(scope,receive,send)
        except ProviderError as e:
            return await JSONResponse({'error':{'code':e.code}},403)(scope,receive,send)
