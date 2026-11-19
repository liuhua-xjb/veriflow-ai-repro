"""Default Phase P routes never activate a paid service."""
import json,os,sqlite3
from fastapi import APIRouter,Request
from typing import Literal,Annotated
from pydantic import Field
from .contracts import StrictModel
from .g4r2_policy import ROOT,ID,POLICY,authorization,phase_gate,dryrun
from .g4r2_approval import R2ApprovalSession
from .g4r2_ledger import G4R2Ledger
from .g4r2_provider import G4R2Provider
from .g4r2_service import G4R2Service
from .llm import ProviderError
from .registry import RegistryError

router=APIRouter()
class MainRequest(StrictModel):seed:Annotated[int,Field(strict=True,ge=42,le=46)]
class ExtraRequest(StrictModel):intent:Literal['recall_quality','latency']

def readonly_budget():
    path=ROOT/'runtime/deepseek_g4r2.sqlite3'
    if not path.is_file():return None
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        payload=json.loads(db.execute('SELECT payload FROM policy').fetchone()[0])
        if payload!={'mode':'REAL_LLM',**POLICY}:raise ProviderError('G4R2_LEDGER_NAMESPACE_MISMATCH')
        n,t,c=db.execute('SELECT COUNT(*),COALESCE(SUM(tokens),0),COALESCE(SUM(microusd),0) FROM slots').fetchone()
        return {'requests_reserved':n,'tokens_reserved':t,'usd_reserved':c/1e6,'requests_remaining':12-n,'tokens_remaining':50000-t,'usd_remaining':(50000-c)/1e6,
            'sent_or_unknown':db.execute("SELECT COUNT(*) FROM slots WHERE status!='HELD'").fetchone()[0],
            'stop_reason':db.execute('SELECT stop_reason FROM control').fetchone()[0],
            'units':[{'unit_key':k,'run_id':r,'state':s,'reason':e} for k,r,s,e in db.execute('SELECT * FROM units ORDER BY rowid')],
            'supplier_actual_bill_usd':None}

def attach(state,session):
    if os.environ.get('G4R2_BACKEND_STARTED')=='1':
        phase_gate()
        if type(session)is not R2ApprovalSession:raise ProviderError('G4R2_LOCAL_BOOTSTRAP_REQUIRED')
        session.check();ledger=G4R2Ledger(ROOT/'runtime/deepseek_g4r2.sqlite3');ledger.recover()
        state.g4r2_service=G4R2Service(state,ledger,G4R2Provider(ledger,state))

def service(state):
    value=getattr(state,'g4r2_service',None)
    if not value:raise ProviderError('AWAITING_CHATGPT_PREKEY_REVIEW')
    return value

@router.get('/api/g4r2/status')
def status(request:Request):
    code='AWAITING_CHATGPT_PREKEY_REVIEW'
    try:
        authorization();phase_gate(local_switch=False)
        code='READY' if hasattr(request.app.state,'g4r2_service') else 'G4R2_LOCAL_START_REQUIRED'
        session=getattr(request.app.state,'g4r2_approval',None)
        if session:session.check()
    except ProviderError as e:code=e.code
    item=getattr(request.app.state,'g4r2_service',None)
    budget=item.ledger.snapshot() if item else readonly_budget()
    if budget:
        for entry in budget['units']:
            try:run=request.app.state.orchestration.get(entry['run_id'])
            except RegistryError:continue
            entry['roles']={name:node['state'] for name,node in run['nodes'].items()}
            entry['verification_status']=run.get('verification_status')
            entry['science_run_id']=run.get('science_run_id')
            entry['mode']=run.get('execution_mode')
    return {'status':code,'authorization_id':ID,'expires_at':'2026-11-01T21:26:58+08:00','limits':POLICY,
        'budget':budget,'active_run_ids':list(item.tasks) if item else [],
        'mode':item.provider.mode if item else 'PHASE_P_NO_KEY','approval_required':True,'key_read':False,
        'matrix':dryrun(),'first_round':'HISTORICAL_ONLY_SEPARATE_LEDGER'}

@router.post('/api/g4r2/main')
async def main(value:MainRequest,request:Request):
    try:return await service(request.app.state).start('main:'+str(value.seed))
    except ProviderError as e:raise RegistryError(403 if 'APPROVAL' in e.code or e.code=='AWAITING_CHATGPT_PREKEY_REVIEW' else 409,e.code)

@router.post('/api/g4r2/extra')
async def extra(value:ExtraRequest,request:Request):
    try:return await service(request.app.state).start('extra:'+value.intent)
    except ProviderError as e:raise RegistryError(403 if 'APPROVAL' in e.code or e.code=='AWAITING_CHATGPT_PREKEY_REVIEW' else 409,e.code)

@router.post('/api/g4r2/{run_id}/cancel')
async def cancel(run_id:str,request:Request):
    try:return service(request.app.state).cancel(run_id)
    except ProviderError as e:raise RegistryError(403 if 'APPROVAL' in e.code or e.code=='AWAITING_CHATGPT_PREKEY_REVIEW' else 409,e.code)
