"""Offline experiment contracts only. No transport, Key access or budget mutation.

Not imported by any app route or real Provider. Future paid execution requires a
new receipt/ledger and reviewed integration; this module cannot authorize it.
"""
import copy,json,math,re
from decimal import Decimal,ROUND_CEILING
from typing import Annotated,Literal
from pydantic import Field
from .contracts import StrictModel
from .deepseek_contracts import PlanProposal,ReviewProposal,PLANNER_EXAMPLE,REVIEW_EXAMPLE,enforce_review
from .deepseek_real_provider import request_body,input_bound
from .science_contracts import DATASET_SHA
from .workflow_models import digest
from .llm import ProviderError

MODE='REPEAT_PREPARATION'
AUTHORIZATION_STATUS='AWAITING_NEW_COST_AUTHORIZATION'
PROMPT_VERSIONS={'planner':'repeat-planner-v1','reviewer':'repeat-reviewer-v1'}
BINDING_VERSION='repeat-binding-v1'
MODELS=['dummy','logistic','random_forest']
METRICS=['precision','recall','f1','macro_f1','pr_auc_average_precision','roc_auc']
TASKS={'balanced_comparison':'比较三种模型，平衡验证结果与运行成本。',
       'recall_quality':'比较三种模型，优先分析故障召回和可信证据。',
       'latency':'比较三种模型，优先关注运行时延及资源开销。'}
Seed=Annotated[int,Field(strict=True,ge=42,le=46)]
Sha=Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')]
Evidence=Annotated[str,Field(pattern=r'^run_[a-f0-9]{32}:[1-9][0-9]{0,5}:[a-f0-9]{64}$')]

class RepeatUnit(StrictModel):
    seed:Seed
    intent:Literal['balanced_comparison','recall_quality','latency']

class RepeatReviewSummary(StrictModel):
    seed:Seed
    data_sha256:Literal[DATASET_SHA]
    train_split_sha256:Sha
    test_split_sha256:Sha
    status:Literal['VERIFIED','REJECTED','UNVERIFIED']
    evidence_ids:list[Evidence]=Field(min_length=1,max_length=8)
    repairs_remaining:Literal[0]=0
    metrics:dict[str,dict[str,float]]

def review_summary(unit,summary):
    parsed=RepeatReviewSummary.model_validate(summary)
    if parsed.seed!=unit.seed:raise ProviderError('REVIEW_SEED_MISMATCH')
    ids=parsed.evidence_ids
    if len(set(ids))!=len(ids) or len({x.split(':')[0] for x in ids})!=1:raise ProviderError('EVIDENCE_SCOPE_MISMATCH')
    # Reuse the existing finite metric/allowlist validation; never upload raw rows.
    request_body('reviewer',{k:summary[k] for k in ('status','evidence_ids','repairs_remaining','metrics')})
    return parsed

def bound_plan_schema(unit):
    schema=copy.deepcopy(PlanProposal.model_json_schema())
    schema['properties']['seed']={'type':'integer','const':unit.seed}
    return schema

def repeat_body(unit,role,summary=None):
    if not isinstance(unit,RepeatUnit):raise ProviderError('TRUSTED_REPEAT_UNIT_REQUIRED')
    if role not in PROMPT_VERSIONS:raise ProviderError('INVALID_ROLE')
    body=request_body('planner',{'intent':unit.intent})
    if role=='planner':
        if summary is not None:raise ProviderError('NO_FREE_TEXT_OR_UPLOADS')
        schema=bound_plan_schema(unit)
        example={**copy.deepcopy(PLANNER_EXAMPLE),'seed':unit.seed,'models':MODELS.copy(),
            'tools':['data_profiler','leakage_check','model_trainer','model_evaluator','report_exporter'],'reason':unit.intent}
        instruction='You are Planner. Return only json. Use the exact experiment seed and all three required models. Choose permitted scheduling and verification independently; dynamic is not guaranteed. No code, URLs, uploads or Hard Gate overrides. '
        user={'task':TASKS[unit.intent],'intent':unit.intent,'experiment_seed':unit.seed,'required_models':MODELS}
    else:
        parsed=review_summary(unit,summary)
        schema=ReviewProposal.model_json_schema();example=REVIEW_EXAMPLE
        instruction='You are independent Reviewer. Return only json. Use only supplied evidence IDs. Never accept a rejected Hard Gate or grant retries. Experimental seed and split hashes are trusted context, not values you may rewrite. '
        user=parsed.model_dump()
    body['messages']=[{'role':'system','content':instruction+'Example json: '+json.dumps(example)+' Schema: '+json.dumps(schema)},
                      {'role':'user','content':json.dumps(user,sort_keys=True)}]
    return body

def validate_plan(raw,unit):
    proposal=PlanProposal.model_validate_json(raw)
    if proposal.seed!=unit.seed:raise ProviderError('PLAN_SEED_MISMATCH')
    if set(proposal.models)!=set(MODELS):raise ProviderError('PRIMARY_THREE_MODEL_CONTRACT_FAILED')
    if proposal.reason!=unit.intent:raise ProviderError('PLAN_INTENT_MISMATCH')
    return proposal  # The supplier output is never patched or relabelled.

def validate_review(raw,unit,summary):
    checked=review_summary(unit,summary)
    proposal=ReviewProposal.model_validate_json(raw)
    return enforce_review(proposal,checked.status,checked.evidence_ids,0)

def validate_simulated_response(data,unit,role,summary=None):
    """Pure TEST_ONLY parser for HTTP fixtures; cannot mint REAL_LLM events."""
    body=repeat_body(unit,role,summary);bound=reviewer_bound(unit) if role=='reviewer' else input_bound(body)
    usage=data['usage'];a,b,t=(usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens'))
    if any(type(x) is not int or x<0 for x in (a,b,t)) or a+b!=t or a>bound or b>800:raise ProviderError('USAGE_INVALID')
    item=data['choices'][0];message=item['message']
    if data['model'] not in {'deepseek-flash','DeepSeek-V4.1-Flash-1017'} or item['finish_reason']!='stop' or message.get('role')!='assistant' or message.get('tool_calls') or message.get('refusal'):raise ProviderError('MODEL_OR_FINISH_REJECTED')
    raw=message['content']
    if not isinstance(raw,str) or not raw.strip() or len(raw.encode('utf-8'))>65536:raise ProviderError('INVALID_CONTENT')
    proposal=validate_plan(raw,unit) if role=='planner' else validate_review(raw,unit,summary)
    return {'mode':'SIMULATED','test_label':'TEST_ONLY','seed':unit.seed,'role':role,
        'binding_version':BINDING_VERSION,'prompt_version':PROMPT_VERSIONS[role],
        'schema_version':proposal.schema_version,'proposal':proposal.model_dump(),'usage':usage,
        'request_digest':digest(body),'supplier_actual_bill_usd':None,'official_response_received':False}

def maximal_review_summary(seed):
    return {'seed':seed,'data_sha256':DATASET_SHA,'train_split_sha256':'f'*64,'test_split_sha256':'f'*64,
        'status':'UNVERIFIED','evidence_ids':['run_'+'f'*32+':'+str(999991+i)+':'+'f'*64 for i in range(8)],
        'repairs_remaining':0,'metrics':{m:{k:0.0 for k in METRICS} for m in MODELS}}

def reviewer_bound(unit):
    # Every variable string is closed ASCII: 8 receipt IDs <=109 bytes, 64-byte
    # SHA fields, finite enum keys/status. Reserve 24 chars per finite float
    # instead of the 3-char 0.0 exemplar, plus existing 256-byte token guard.
    return input_bound(repeat_body(unit,'reviewer',maximal_review_summary(unit.seed)))+18*(24-len('0.0'))

def micro_usd(bound):
    return int((Decimal(bound)*Decimal('0.30')+Decimal(800)*Decimal('1.20')).to_integral_value(rounding=ROUND_CEILING))

def unit_budget(unit):
    planner=input_bound(repeat_body(unit,'planner'));reviewer=reviewer_bound(unit)
    return {'seed':unit.seed,'intent':unit.intent,'planner_input_bound':planner,'reviewer_input_bound':reviewer,
        'requests':2,'reserved_tokens':planner+reviewer+1600,
        'reserved_microusd':micro_usd(planner)+micro_usd(reviewer),
        'usd_upper':(micro_usd(planner)+micro_usd(reviewer))/1e6,
        'request_sha256':digest(repeat_body(unit,'planner'))}

def budget_dryrun():
    units=[RepeatUnit(seed=s,intent=i) for s,i in [(43,'balanced_comparison'),(44,'recall_quality'),(45,'latency'),(46,'balanced_comparison')]]
    rows=[unit_budget(u) for u in units];bridge=unit_budget(RepeatUnit(seed=42,intent='balanced_comparison'))
    def total(values):
        return {'requests':sum(x['requests'] for x in values),'reserved_tokens':sum(x['reserved_tokens'] for x in values),
                'usd_upper':sum(x['reserved_microusd'] for x in values)/1e6}
    four=total(rows);five=total(rows+[bridge])
    max_input=max(max(r['planner_input_bound'],r['reviewer_input_bound']) for r in rows+[bridge])
    # At most two optional individually authorized requests, with conservative
    # max-role input allowance; no budget is actually allocated or consumed.
    proposal_tokens=five['reserved_tokens']+2*(max_input+800)
    proposal_usd=five['usd_upper']+2*micro_usd(max_input)/1e6
    return {'mode':MODE,'status':AUTHORIZATION_STATUS,'new_paid_requests':0,'key_access':False,
        'binding_version':BINDING_VERSION,'prompt_versions':PROMPT_VERSIONS,
        'schema_sha256':digest({'plan':PlanProposal.model_json_schema(),'review':ReviewProposal.model_json_schema(),
            'unit':RepeatUnit.model_json_schema(),'review_input':RepeatReviewSummary.model_json_schema()}),
        'price_checked_date':'2026-10-31','input_usd_per_million_peak_cache_miss':0.30,'output_usd_per_million_peak':1.20,
        'output_per_request':800,'per_request_timeout_seconds':30,'units':rows,'four_extra_cycles':four,
        'fresh_seed42_bridge':bridge,'same_version_five_cycles':five,
        'optional_failure_requests':2,'optional_failure_requests_input_bound_each':max_input,
        'five_plus_two_requests_upper':{'requests':12,'reserved_tokens':proposal_tokens,'usd_upper':round(proposal_usd,6)},
        'proposal_only_not_authorized':{'max_requests':12,'max_reserved_tokens':max(50000,math.ceil(proposal_tokens/1000)*1000),
            'max_usd':0.05,'validity':'UNSET_REQUIRES_USER_APPROVAL','new_receipt_and_ledger_required':True},
        'legacy_uniform_four_cycle_reference':{'requests':8,'reserved_tokens':32220,'usd_upper':0.015428},
        'caveats':['No new authority; not wired to the existing paid Provider.',
            'Existing seed42 uses an older prompt; bridge required for same-version n=5.',
            'Varied intents confound inference; no guaranteed dynamic choice or significant speedup.',
            'Reject mismatched seeds/subsets; preserve all failures, never replace or re-label outputs.',
            'Price recheck and exact final outgoing body bound required before any future dispatch.']}
