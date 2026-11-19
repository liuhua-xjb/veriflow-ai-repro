"""Preflight-only closed roles. No model output is executable code."""
from typing import Literal
from pydantic import Field,model_validator
from .contracts import StrictModel

class DeepSeekSettings(StrictModel):
    base_url: Literal['https://api.deepseek.com']='https://api.deepseek.com'
    model: Literal['deepseek-flash']='deepseek-flash'
    real_calls_authorized: Literal[False]=False
    max_requests:int=Field(default=6,ge=1,le=6)
    max_input_tokens:int=Field(default=4000,ge=128,le=4000)
    max_output_tokens:int=Field(default=800,ge=32,le=800)
    max_total_tokens:int=Field(default=12000,ge=1,le=12000)
    max_estimated_spend_usd:float|None=Field(default=None,gt=0,le=1,allow_inf_nan=False)
    max_wall_time_seconds:float=Field(default=30.,gt=0,le=30,allow_inf_nan=False)

class PlanProposal(StrictModel):
    schema_version:Literal['ds-plan-v1']='ds-plan-v1'
    dataset:Literal['ai4i2020']
    target:Literal['Machine failure']
    seed:Literal[42,43,44,45,46]
    models:list[Literal['dummy','logistic','random_forest']]=Field(min_length=1,max_length=3)
    strategy:Literal['fixed_serial','dynamic']
    verification:Literal['verify_only','verify_and_repair']
    tools:list[Literal['data_profiler','leakage_check','model_trainer','model_evaluator','report_exporter']]=Field(min_length=4,max_length=5)
    reason:Literal['latency','recall_quality','balanced_comparison']

    @model_validator(mode='after')
    def permissions(self):
        if len(set(self.models))!=len(self.models) or len(set(self.tools))!=len(self.tools):raise ValueError('duplicate_selection')
        if not {'data_profiler','leakage_check','model_trainer','model_evaluator'}<=set(self.tools):raise ValueError('missing_required_tools')
        if ('report_exporter' in self.tools)!=(len(self.models)==3):raise ValueError('export_requires_three_models')
        return self

class ReviewProposal(StrictModel):
    schema_version:Literal['ds-review-v1']='ds-review-v1'
    decision:Literal['accept_evidence','reject','recommend_same_tool_reexecution']
    evidence_ids:list[str]=Field(min_length=1,max_length=8)
    reason:Literal['metrics_consistent','hard_gate_rejected','insufficient_evidence','transient_output_mismatch']

PLANNER_EXAMPLE={'dataset':'ai4i2020','target':'Machine failure','seed':42,'models':['dummy'],
 'strategy':'fixed_serial','verification':'verify_only','tools':['data_profiler','leakage_check','model_trainer','model_evaluator'],'reason':'latency'}
REVIEW_EXAMPLE={'decision':'reject','evidence_ids':['evidence-example'],'reason':'hard_gate_rejected'}

def compile_proposal(plan,controller):
    from .science_api import definition
    from .workflow_models import WorkflowSpec
    spec=definition(plan.seed,'safe',plan.strategy=='fixed_serial')
    chosen=set(plan.models)
    spec['nodes']=[n for n in spec['nodes'] if n['id'] in {'profile','leakage'} or n['id']=='export' and len(chosen)==3 or n['id'].removeprefix('train_').removeprefix('eval_') in chosen]
    ids={n['id'] for n in spec['nodes']}
    spec['edges']=[e for e in spec['edges'] if e['source'] in ids and e['target'] in ids]
    if len(chosen)!=3:spec['outputs']={m:{'source':'node','node_id':'eval_'+m,'field':'artifact_id'} for m in chosen}
    model=WorkflowSpec.model_validate(spec)
    controller.validate_workflow('ds_proposal',1,model)
    return model

def enforce_review(review,status,evidence_ids,repairs_remaining):
    if not set(review.evidence_ids)<=set(evidence_ids):raise ValueError('unknown_evidence')
    if status!='VERIFIED' and review.decision=='accept_evidence':raise ValueError('hard_gate_cannot_be_overridden')
    if review.decision=='recommend_same_tool_reexecution' and repairs_remaining<=0:raise ValueError('repair_budget_exhausted')
    return review
