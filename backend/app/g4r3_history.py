"""Read-only inheritance of two audited REAL_LLM units; never transfers credit."""
import hashlib, json, sqlite3, sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from .workflow_models import digest
from .deepseek_repeat_prep import RepeatUnit, repeat_body, PROMPT_VERSIONS, BINDING_VERSION, MODELS
from .llm import ProviderError

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'reports/G4/deepseek/real/g4r3'
PINS = OUT/'HISTORY_PINS.json'
PINS_SHA = '37256f9810b8b3dd74c1ca395061fe551cc333c3f411c38606cabd8774924132'


def chain(records):
    previous = '0'*64
    for seq, r in enumerate(records, 1):
        payload = {k:v for k,v in r.items() if k not in {'kind','previous_hash','receipt_hash','evidence_id'}}
        h = digest({'kind':r['kind'],'payload':payload,'previous_hash':previous})
        if r['seq'] != seq or r['previous_hash'] != previous or h != r['receipt_hash'] or r['evidence_id'] != f"{r['run_id']}:{seq}:{h}":
            raise ProviderError('G4R3_HISTORICAL_CHAIN_INVALID')
        previous = h
    if not records or records[-1]['kind'] != 'RunVerificationFinished' or records[-1].get('status') != 'VERIFIED':
        raise ProviderError('G4R3_HISTORICAL_NOT_VERIFIED')
    return previous


def validate_record(d, seed):
    parent, science = d['run'], d['science']
    child = science['run']
    if (d['mode'] != 'REAL_LLM' or d.get('test_label') is not None or parent['execution_mode'] != 'REAL_LLM_G4R2'
        or parent['llm_inference'] != 'REAL_LLM' or parent['seed'] != seed or parent['unit_key'] != f'main:{seed}'
        or parent['science_run_id'] != child['run_id'] or parent['state'] != 'SUCCEEDED'
        or parent['verification_status'] != 'VERIFIED' or child['state'] != 'SUCCEEDED'
        or child['verification_status'] != 'VERIFIED' or len(child['nodes']) != 9
        or any(n['state'] != 'SUCCEEDED' for n in child['nodes'].values())):
        raise ProviderError('G4R3_HISTORICAL_BINDING_INVALID')
    ph, ch = chain(d['role_receipts']), chain(science['verification'])
    if any(r['run_id'] != parent['run_id'] for r in d['role_receipts']) or any(r['run_id'] != child['run_id'] for r in science['verification']):
        raise ProviderError('G4R3_HISTORICAL_CROSS_RUN')
    roles = {r['receipt']['node_id']:r['receipt'] for r in d['role_receipts'] if r['kind']=='G4R2RoleResponse'}
    if set(roles) != {'planner','reviewer'}:raise ProviderError('G4R3_HISTORICAL_ROLES_REQUIRED')
    for role, receipt in roles.items():
        if (receipt['mode'] != 'REAL_LLM' or receipt.get('test_label') is not None or receipt['run_id'] != parent['run_id']
            or receipt['seed'] != seed or receipt['unit_key'] != f'main:{seed}' or not receipt['official_response_received']
            or not receipt['official_model_response_received'] or receipt['prompt_version'] != PROMPT_VERSIONS[role]
            or receipt['binding_version'] != BINDING_VERSION or receipt['provider'] != 'deepseek'):
            raise ProviderError('G4R3_HISTORICAL_ROLE_INVALID')
    plan = roles['planner']['proposal']
    if plan['seed'] != seed or set(plan['models']) != set(MODELS) or plan['reason'] != 'balanced_comparison' or roles['planner']['request_digest'] != digest(repeat_body(RepeatUnit(seed=seed,intent='balanced_comparison'),'planner')):
        raise ProviderError('G4R3_COHORT_CHANGED')
    ids = {r['evidence_id'] for r in science['verification']}
    review = roles['reviewer']['proposal']
    if review['decision'] != 'accept_evidence' or not review['evidence_ids'] or not set(review['evidence_ids']) <= ids:
        raise ProviderError('G4R3_HISTORICAL_REVIEW_INVALID')
    return {'seed':seed,'parent':parent['run_id'],'child':child['run_id'],'state':'SUCCEEDED','verification_status':'VERIFIED',
            'parent_chain_head':ph,'child_chain_head':ch,'mode':'HISTORICAL_REAL_LLM_G4R2','new_budget_requests':0,
            'strategy':plan['strategy'],'verification':plan['verification']}


def verify():
    try:
        if hashlib.sha256(PINS.read_bytes()).hexdigest() != PINS_SHA:raise ProviderError('G4R3_HISTORY_PINS_CHANGED')
        pins = json.loads(PINS.read_text(encoding='utf-8'))
        for name, h in pins['files'].items():
            p = (ROOT/name).resolve()
            if not p.is_relative_to(ROOT) or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != h:
                raise ProviderError('G4R3_HISTORY_OR_COHORT_CHANGED')
        if sys.version != pins['python'] or any(version(k) != v for k,v in pins['packages'].items()):raise ProviderError('G4R3_COHORT_ENVIRONMENT_CHANGED')
        baseline = OUT/'baseline.json'
        if hashlib.sha256(baseline.read_bytes()).hexdigest() != pins['baseline_sha256']:raise ProviderError('G4R3_BASELINE_CHANGED')
        old = json.loads(baseline.read_text(encoding='utf-8'))
        # All pre-existing rows must remain. R3 may append its own later, never
        # rewrite historical rows. Two old fee ledgers remain exactly equal.
        for name, tables in old['databases'].items():
            with sqlite3.connect((ROOT/'runtime'/name).as_uri()+'?mode=ro',uri=True) as db:
                for table, rows in tables.items():
                    actual = Counter(tuple(r) for r in db.execute('SELECT * FROM "'+table+'"'))
                    expected = Counter(tuple(r) for r in rows)
                    if (actual != expected if name.startswith('deepseek_') else bool(expected-actual)):
                        raise ProviderError('G4R3_HISTORICAL_DATABASE_CHANGED')
        oldready = json.loads((ROOT/'reports/G4/deepseek/real/g4r2/READY.json').read_text())
        for name,h in oldready['source_sha256'].items():
            if name.startswith('backend/app/') and name != 'backend/app/main.py' and pins['files'].get(name) != h:
                raise ProviderError('G4R3_COHORT_SOURCE_CHANGED')
        result = []
        for seed in (42,43):
            matches = []
            for p in (ROOT/'reports/G4/deepseek/real/g4r2/real-runs').glob('run*.json'):
                if p.name.endswith('-science.json'):continue
                d=json.loads(p.read_text(encoding='utf-8'))
                if 'run' in d and d['run'].get('seed') == seed:matches.append(d)
            if len(matches) != 1:raise ProviderError('G4R3_HISTORICAL_UNIT_MISSING')
            result.append(validate_record(matches[0], seed))
        return result
    except ProviderError:raise
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):raise ProviderError('G4R3_HISTORY_UNREADABLE') from None
