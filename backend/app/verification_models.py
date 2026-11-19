"""Versioned closed verification contracts; model opinions have no voting power."""
from typing import Literal
from pydantic import Field, model_validator
from .contracts import StrictModel

RULES = ('output_schema', 'numeric_recompute', 'json_fields_recompute', 'required_fields')


class VerificationRule(StrictModel):
    rule_id: Literal['output_schema', 'numeric_recompute', 'json_fields_recompute', 'required_fields']
    version: Literal['1.0.0'] = '1.0.0'
    required: Literal[True] = True


class FeedbackPolicy(StrictModel):
    max_repairs: int = Field(default=2, ge=0, le=2)
    max_total_attempts: int = Field(default=300, ge=1, le=300)
    deadline_seconds: float = Field(default=30., gt=0, le=30, allow_inf_nan=False)
    no_progress_limit: int = Field(default=2, ge=2, le=3)
    token_budget: Literal[0] = 0
    cost_budget: Literal[0] = 0


class VerificationPolicy(StrictModel):
    version: Literal['1.0.0'] = '1.0.0'
    mode: Literal['execution_only', 'verify_only', 'verify_and_repair'] = 'execution_only'
    rules: list[VerificationRule] = Field(default_factory=lambda: [VerificationRule(rule_id=r) for r in RULES], min_length=4, max_length=4)
    abs_tolerance: float = Field(default=1e-9, ge=0, le=1e-6, allow_inf_nan=False)
    rel_tolerance: float = Field(default=1e-12, ge=0, le=1e-9, allow_inf_nan=False)
    feedback: FeedbackPolicy = Field(default_factory=FeedbackPolicy)

    @model_validator(mode='after')
    def complete(self):
        if {r.rule_id for r in self.rules} != set(RULES):
            raise ValueError('all_hard_rules_required')
        return self


class FailureReason(StrictModel):
    code: Literal['schema_invalid', 'numeric_mismatch', 'field_result_mismatch', 'required_fields_missing']
    path: Literal['$', 'count', 'minimum', 'maximum', 'mean', 'missing/valid', 'document']
    recoverable: bool


class VerificationResult(StrictModel):
    rule_id: VerificationRule.model_fields['rule_id'].annotation
    version: Literal['1.0.0'] = '1.0.0'
    passed: bool
    failure: FailureReason | None = None
    difference: dict[str, float] = Field(default_factory=dict, max_length=4)
    input_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    output_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    timestamp: str
