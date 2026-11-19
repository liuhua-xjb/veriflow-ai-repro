"""Default Phase P routes never activate a paid service."""
import json,os,sqlite3
from fastapi import APIRouter,Request
from typing import Literal,Annotated
from pydantic import Field
from .contracts import StrictModel
from .g4r3_policy import ROOT,ID,POLICY,authorization,phase_gate,dryrun,progression_review
from .g4r3_history import verify as verify_history
from .g4r3_approval import R3ApprovalSession
from .g4r3_ledger import G4R3Ledger
from .g4r3_provider import G4R3Provider
from .g4r3_service import G4R3Service
from .llm import ProviderError
from .registry import RegistryError

router=APIRouter()
class MainRequest(StrictModel):seed:Annotated[int,Field(strict=True,ge=44,le=46)]
class ExtraRequest(StrictModel):intent:Literal['recall_quality','latency']

def readonly_budget():
    path=ROOT/'runtime/deepseek_g4r3.sqlite3'
    if not path.is_file():return None
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        payload=json.loads(db.execute('SELECT payload FROM policy').fetchone()[0])
        if payload!={'mode':'REAL_LLM',**POLICY}:raise ProviderError('G4R3_LEDGER_NAMESPACE_MISMATCH')
        n,t,c=db.execute('SELECT COUNT(*),COALESCE(SUM(tokens),0),COALESCE(SUM(microusd),0) FROM slots').fetchone()
        return {'requests_reserved':n,'tokens_reserved':t,'usd_reserved':c/1e6,'requests_remaining':8-n,'tokens_remaining':32000-t,'usd_remaining':(30000-c)/1e6,
            'sent_or_unknown':db.execute("SELECT COUNT(*) FROM slots WHERE status!='HELD'").fetchone()[0],
            'stop_reason':db.execute('SELECT stop_reason FROM control').fetchone()[0],
            'units':[{'unit_key':k,'run_id':r,'state':s,'reason':e} for k,r,s,e in db.execute('SELECT * FROM units ORDER BY rowid')],
            'supplier_actual_bill_usd':None}

def attach(state,session):
    if os.environ.get('G4R3_BACKEND_STARTED')=='1':
        phase_gate()
        if type(session)is not R3ApprovalSession:raise ProviderError('G4R3_LOCAL_BOOTSTRAP_REQUIRED')
        session.check();ledger=G4R3Ledger(ROOT/'runtime/deepseek_g4r3.sqlite3');ledger.recover()
        state.g4r3_service=G4R3Service(state,ledger,G4R3Provider(ledger,state))

def service(state):
    value=getattr(state,'g4r3_service',None)
    if not value:raise ProviderError('AWAITING_CHATGPT_G4R3_PREKEY_REVIEW')
    return value

@router.get('/api/g4r3/status')
def status(request:Request):
    code='AWAITING_CHATGPT_G4R3_PREKEY_REVIEW'
    try:
        authorization();phase_gate(local_switch=False)
        code='READY' if hasattr(request.app.state,'g4r3_service') else 'G4R3_LOCAL_START_REQUIRED'
        session=getattr(request.app.state,'g4r3_approval',None)
        if session:session.check()
    except ProviderError as e:code=e.code
    item=getattr(request.app.state,'g4r3_service',None)
    budget=item.ledger.snapshot() if item else readonly_budget()
    if budget:
        for entry in budget['units']:
            try:run=request.app.state.orchestration.get(entry['run_id'])
            except RegistryError:continue
            entry['roles']={name:node['state'] for name,node in run['nodes'].items()}
            entry['verification_status']=run.get('verification_status')
            entry['science_run_id']=run.get('science_run_id')
            entry['mode']=run.get('execution_mode')
    history=[];history_error=None;available=[]
    try:history=verify_history()
    except ProviderError as e:history_error=e.code
    entries=budget['units'] if budget else []
    done={x['unit_key']:x for x in entries if x['state']=='SUCCEEDED'}
    claimed={x['unit_key'] for x in entries}
    if history and not (budget and budget['stop_reason']) and not any(x['state']=='RUNNING' for x in entries):
        if 'main:44' not in claimed:available.append('main:44')
        if 'main:44' in done:
            reviewed=False
            try:progression_review(done['main:44']['run_id']);reviewed=True
            except ProviderError:pass
            if reviewed:
                if 'main:45' not in claimed:available.append('main:45')
                if 'main:45' in done and 'main:46' not in claimed:available.append('main:46')
        if all(f'main:{seed}' in done for seed in (44,45,46)):
            available.extend(key for key in ('extra:recall_quality','extra:latency') if key not in claimed)
    return {'status':code,'authorization_id':ID,'expires_at':'2026-11-09T13:42:16+08:00','limits':POLICY,
        'budget':budget,'active_run_ids':list(item.tasks) if item else [],
        'mode':item.provider.mode if item else 'PHASE_P_NO_KEY','approval_required':True,'key_read':False,
        'matrix':dryrun(),'first_round':'HISTORICAL_ONLY_SEPARATE_LEDGER','historical_main':history,
        'history_error':history_error,'available_units':available}

@router.post('/api/g4r3/main')
async def main(value:MainRequest,request:Request):
    try:return await service(request.app.state).start('main:'+str(value.seed))
    except ProviderError as e:raise RegistryError(403 if 'APPROVAL' in e.code or e.code=='AWAITING_CHATGPT_G4R3_PREKEY_REVIEW' else 409,e.code)

@router.post('/api/g4r3/extra')
async def extra(value:ExtraRequest,request:Request):
    try:return await service(request.app.state).start('extra:'+value.intent)
    except ProviderError as e:raise RegistryError(403 if 'APPROVAL' in e.code or e.code=='AWAITING_CHATGPT_G4R3_PREKEY_REVIEW' else 409,e.code)

@router.post('/api/g4r3/{run_id}/cancel')
async def cancel(run_id:str,request:Request):
    try:return service(request.app.state).cancel(run_id)
    except ProviderError as e:raise RegistryError(403 if 'APPROVAL' in e.code or e.code=='AWAITING_CHATGPT_G4R3_PREKEY_REVIEW' else 409,e.code)
