"""Independent third authorization; both previous policies and READY stay intact."""
import hashlib,json,os,time
from datetime import datetime
from pathlib import Path
from .llm import ProviderError
from .deepseek_repeat_prep import RepeatUnit,repeat_body,reviewer_bound,micro_usd,maximal_review_summary
from .deepseek_real_provider import input_bound
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'reports/G4/deepseek/real/g4r3'
ID='G4R3-20261108T134216+0800'
RECEIPT=ROOT/'reports/G4/deepseek/real/AUTHORIZATION_RECEIPT_G4R3_20261108_134216.md'
SHA='36f4b0357ee9b45c4c025ef4b388b6bbd8a3dcb80cdee302f8fbd519fe7bc81a'
MATRIX=ROOT/'reports/G4/deepseek/real/G4_REPEAT_REVIEWED_MATRIX.md'
MATRIX_SHA='edb1e2d5297f0f10bf4c92be1c64263eb5053dbfe0f7352fac0b192fbcd7aed3'
START=datetime.fromisoformat('2026-11-08T13:42:16+08:00').timestamp()
EXPIRES=datetime.fromisoformat('2026-11-09T13:42:16+08:00').timestamp()
ENDPOINT='https://api.deepseek.com/chat/completions'
POLICY={'authorization_id':ID,'receipt_sha256':SHA,'matrix_sha256':MATRIX_SHA,'start':START,'expires':EXPIRES,
    'model':'deepseek-flash','endpoint':ENDPOINT,'requests':8,'tokens':32000,'microusd':30000,
    'input':4000,'output':800,'request_timeout':30,'workflow_timeout':180,'input_rate':.30,'output_rate':1.20}

def authorization(now=None):
    if not RECEIPT.is_file() or hashlib.sha256(RECEIPT.read_bytes()).hexdigest()!=SHA:raise ProviderError('G4R3_RECEIPT_MISMATCH')
    if not MATRIX.is_file() or hashlib.sha256(MATRIX.read_bytes()).hexdigest()!=MATRIX_SHA:raise ProviderError('G4R3_MATRIX_MISMATCH')
    t=time.time() if now is None else now
    if t<START:raise ProviderError('AUTHORIZATION_NOT_STARTED')
    if t>=EXPIRES:raise ProviderError('AUTHORIZATION_EXPIRED')
    return POLICY.copy()

def unit(key):
    if key in [f'main:{s}' for s in range(44,47)]:return RepeatUnit(seed=int(key.split(':')[1]),intent='balanced_comparison')
    if key in {'extra:recall_quality','extra:latency'}:return RepeatUnit(seed=42,intent=key.split(':')[1])
    raise ProviderError('UNREGISTERED_EXPERIMENT_UNIT')

def bounds(key):
    u=unit(key);values={'planner':input_bound(repeat_body(u,'planner'))}
    if key.startswith('main:'):values['reviewer']=reviewer_bound(u)
    if any(type(x)is not int or not 0<x<=4000 for x in values.values()):raise ProviderError('PROMPT_INPUT_POLICY_EXCEEDED')
    return values

def dryrun():
    rows=[]
    for key in [f'main:{s}' for s in range(44,47)]+['extra:recall_quality','extra:latency']:
        b=bounds(key);rows.append({'unit_key':key,'seed':unit(key).seed,'intent':unit(key).intent,'input_bounds':b,
            'requests':len(b),'tokens':sum(x+800 for x in b.values()),'microusd':sum(micro_usd(x) for x in b.values()),
            'planner_request_sha256':hashlib.sha256(json.dumps(repeat_body(unit(key),'planner'),sort_keys=True).encode()).hexdigest(),
            'reviewer_template_sha256':hashlib.sha256(json.dumps(repeat_body(unit(key),'reviewer',maximal_review_summary(unit(key).seed)),sort_keys=True).encode()).hexdigest() if key.startswith('main:') else None})
    totals={'requests':sum(r['requests'] for r in rows),'tokens':sum(r['tokens'] for r in rows),'microusd':sum(r['microusd'] for r in rows)}
    if any(totals[k]>POLICY[k] for k in totals):raise ProviderError('WHOLE_BATCH_BUDGET_EXCEEDED')
    return {'authorization_id':ID,'units':rows,'totals':totals,'usd_upper':totals['microusd']/1e6,
        'main_intent':'balanced_comparison','extras':'Planner-only; only after all five main units succeed',
        'status':'AWAITING_CHATGPT_G4R3_PREKEY_REVIEW','new_paid_requests':0}

def verify_ready():
    path=OUT/'READY.json'
    if not path.is_file():raise ProviderError('G4R3_READY_MISSING')
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('tests_passed')is not True or data.get('authorization_sha256')!=SHA or not data.get('source_sha256'):raise ProviderError('G4R3_READY_INVALID')
    for name,h in data['source_sha256'].items():
        source=(ROOT/name).resolve()
        if not source.is_relative_to(ROOT) or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=h:raise ProviderError('G4R3_READY_SOURCE_CHANGED')
    if data.get('budget')!=dryrun():raise ProviderError('G4R3_READY_BODY_OR_BUDGET_CHANGED')
    return hashlib.sha256(path.read_bytes()).hexdigest()

def phase_gate(*,local_switch=True):
    authorization()
    from .g4r3_history import verify
    verify()
    ready_sha=verify_ready();release=OUT/'CHATGPT_PREKEY_RELEASE.json'
    if not release.is_file():raise ProviderError('AWAITING_CHATGPT_G4R3_PREKEY_REVIEW')
    try:d=json.loads(release.read_text(encoding='utf-8'))
    except (ValueError,OSError):raise ProviderError('AWAITING_CHATGPT_G4R3_PREKEY_REVIEW') from None
    if not isinstance(d,dict) or d.get('status')!='PASS' or d.get('authorization_id')!=ID or str(d.get('ready_sha256','')).lower()!=ready_sha:raise ProviderError('AWAITING_CHATGPT_G4R3_PREKEY_REVIEW')
    review=ROOT/str(d.get('review_path',''))
    if not review.resolve().is_relative_to(OUT) or not review.is_file() or hashlib.sha256(review.read_bytes()).hexdigest()!=str(d.get('review_sha256','')).lower():raise ProviderError('AWAITING_CHATGPT_G4R3_PREKEY_REVIEW')
    try:
        checked=datetime.fromisoformat(d['price_checked_at'])
        if checked.tzinfo is None or not START<=checked.timestamp()<=time.time()<EXPIRES or d.get('pricing_source')!='https://api-docs.deepseek.com/quick_start/pricing/' or d.get('input_rate')!=.30 or d.get('output_rate')!=1.20:
            raise ValueError()
    except (ValueError,TypeError,KeyError):raise ProviderError('G4R3_PRICE_REVIEW_REQUIRED') from None
    if local_switch and os.environ.get('G4R3_BACKEND_STARTED')!='1':raise ProviderError('G4R3_LOCAL_START_REQUIRED')

def progression_review(run_id):
    """Independent seed44 audit is required before later main units, never minted here."""
    pending='AWAITING_CHATGPT_G4R3_MAIN44_REVIEW'
    try:
        release=json.loads((OUT/'CHATGPT_MAIN44_RELEASE.json').read_text(encoding='utf-8'))
        if release.get('status')!='PASS' or release.get('authorization_id')!=ID or release.get('run_id')!=run_id:raise ValueError()
        run=OUT/'real-runs'/(run_id+'.json')
        if hashlib.sha256(run.read_bytes()).hexdigest()!=str(release.get('run_sha256','')).lower():raise ValueError()
        review=(ROOT/str(release.get('review_path',''))).resolve()
        if not review.is_relative_to(OUT) or hashlib.sha256(review.read_bytes()).hexdigest()!=str(release.get('review_sha256','')).lower():raise ValueError()
        record=json.loads(run.read_text(encoding='utf-8'))
        if record.get('mode')!='REAL_LLM' or record['run'].get('unit_key')!='main:44' or record['run'].get('run_id')!=run_id or record['run'].get('state')!='SUCCEEDED' or record['run'].get('verification_status')!='VERIFIED':raise ValueError()
    except (OSError,ValueError,KeyError,TypeError):raise ProviderError(pending) from None

