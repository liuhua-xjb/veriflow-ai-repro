"""Bounded declarative workflow language. No expressions or executable code."""
import hashlib
import json
import math
import os
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .agents import Identifier
from .contracts import StrictModel
from .registry import RegistryError
from .tools import Fields, Numbers

ValueType = Literal['number', 'integer', 'boolean', 'string', 'numbers', 'strings', 'object']


class InputField(StrictModel):
    type: ValueType
    sensitive: bool = False


class Binding(StrictModel):
    source: Literal['input', 'node', 'literal']
    field: str = Field(default='', max_length=64)
    node_id: Identifier | None = None
    value: Any = None

    @model_validator(mode='after')
    def shape(self):
        if self.source == 'literal':
            if self.field or self.node_id is not None:
                raise ValueError('literal_binding_shape')
            value_type(self.value)
            reject_literal_credentials(self.value)
        elif not self.field or self.value is not None or (self.source == 'node') != (self.node_id is not None):
            raise ValueError('reference_binding_shape')
        return self


class Predicate(StrictModel):
    left: Binding
    op: Literal['eq', 'gt', 'lt', 'exists']
    right: Any = None

    @model_validator(mode='after')
    def scalar(self):
        if self.op != 'exists' and type(self.right) not in {str, int, float, bool}:
            raise ValueError('predicate_requires_scalar')
        if self.right is not None: value_type(self.right)
        return self


class Gate(StrictModel):
    condition: Identifier
    equals: bool


class WorkflowRef(StrictModel):
    workflow_id: Identifier
    version: int = Field(ge=1)


class Node(StrictModel):
    id: Identifier
    kind: Literal['tool_task', 'condition', 'parallel', 'join', 'bounded_loop', 'subworkflow']
    inputs: dict[Identifier, Binding] = Field(default_factory=dict, max_length=16)
    tool: Literal['numeric_summary', 'json_fields', 'data_profiler', 'leakage_check', 'model_trainer', 'model_evaluator', 'report_exporter'] | None = None
    capability: Identifier | None = None
    agent_id: Identifier | None = None
    priority: int = Field(default=0, ge=0, le=100)
    timeout_seconds: float = Field(default=3.0, ge=.001, le=10)
    # Explicit synthetic queue/service delay for demonstrations; not tool latency.
    service_delay_ms: int = Field(default=0, ge=0, le=500)
    predicate: Predicate | None = None
    gate: Gate | None = None
    workflow: WorkflowRef | None = None
    iterations: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode='after')
    def shape(self):
        if self.kind == 'tool_task':
            if not self.tool or not self.capability or self.predicate or self.workflow or self.iterations is not None:
                raise ValueError('tool_node_shape')
        elif self.tool or self.capability or self.agent_id or self.service_delay_ms:
            raise ValueError('control_node_cannot_execute_tools')
        if self.kind == 'condition':
            if not self.predicate or self.inputs or self.workflow or self.iterations is not None:
                raise ValueError('condition_node_shape')
        elif self.predicate:
            raise ValueError('predicate_requires_condition')
        if self.kind in {'bounded_loop', 'subworkflow'}:
            if not self.workflow or (self.kind == 'bounded_loop') != (self.iterations is not None):
                raise ValueError('bounded_control_requires_explicit_reference_and_limit')
        elif self.workflow or self.iterations is not None:
            raise ValueError('unexpected_workflow_reference')
        if self.kind == 'parallel' and self.inputs:
            raise ValueError('parallel_is_an_empty_fork')
        return self


class Edge(StrictModel):
    source: Identifier
    target: Identifier


class WorkflowSpec(StrictModel):
    description: str = Field(default='', max_length=1000)
    input_schema: dict[Identifier, InputField] = Field(default_factory=dict, max_length=16)
    nodes: list[Node] = Field(min_length=1, max_length=32)
    edges: list[Edge] = Field(default_factory=list, max_length=128)
    outputs: dict[Identifier, Binding] = Field(default_factory=dict, max_length=16)
    max_concurrency: int = Field(default=4, ge=1, le=4)
    timeout_seconds: float = Field(default=15.0, ge=.001, le=30)

    @model_validator(mode='after')
    def size(self):
        if len(self.model_dump_json()) > 131072:
            raise ValueError('workflow_size_limit')
        return self


class WorkflowCreate(WorkflowSpec):
    workflow_id: Identifier = Field(default_factory=lambda: 'wf_' + uuid4().hex)


class WorkflowRevision(WorkflowSpec):
    base_version: int = Field(ge=1)


class SchedulerConfig(StrictModel):
    version: Literal['score-v1'] = 'score-v1'
    capability_weight: float = Field(default=10.0, ge=0, le=100)
    priority_weight: float = Field(default=1.0, ge=0, le=100)
    age_weight: float = Field(default=.1, ge=0, le=100)
    load_weight: float = Field(default=20.0, ge=0, le=100)


from .verification_models import VerificationPolicy


class RunCreate(StrictModel):
    verification: VerificationPolicy = Field(default_factory=VerificationPolicy)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)
    workflow_id: Identifier
    workflow_version: int = Field(ge=1)
    inputs: dict[str, Any] = Field(default_factory=dict, max_length=16)
    strategy: Literal['fixed_serial', 'dynamic'] = 'dynamic'
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)


def fail(code):
    raise RegistryError(422, code)


def value_type(value):
    if type(value) is bool: return 'boolean'
    if type(value) in {int, float} and math.isfinite(value) and abs(value) <= 1e100:
        return 'integer' if type(value) is int else 'number'
    if isinstance(value, str) and len(value) <= 2000: return 'string'
    if isinstance(value, list):
        if len(value) <= 1000 and value and all(type(v) in {int, float} for v in value):
            Numbers(values=value)
            return 'numbers'
        if len(value) <= 100 and all(isinstance(v, str) and len(v) <= 100 for v in value): return 'strings'
    if isinstance(value, dict):
        Fields(document=value, required=['_bounded_check'])
        return 'object'
    raise ValueError('unsupported_or_unbounded_value')


def compatible(actual, expected):
    return actual == expected or (actual == 'integer' and expected == 'number')


def reject_literal_credentials(value):
    if isinstance(value, dict):
        reserved={'api_key','authorization','password','secret','access_token','refresh_token'}
        if any(k.lower().replace('-','_') in reserved for k in value):
            raise ValueError('credentials_are_not_workflow_literals')
        for child in value.values(): reject_literal_credentials(child)
    elif isinstance(value,list):
        for child in value: reject_literal_credentials(child)
    elif isinstance(value,str):
        key=os.environ.get('VERIFLOW_LLM_API_KEY','')
        if value.lower().startswith(('bearer ','sk-')) or (key and key in value):
            raise ValueError('credentials_are_not_workflow_literals')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def safe_value(value):
    """String payloads and arbitrary object keys never enter run/event storage."""
    if isinstance(value, str): return '[REDACTED]'
    if isinstance(value, list): return [safe_value(v) for v in value]
    if isinstance(value, dict):
        known = {'count', 'minimum', 'maximum', 'mean', 'valid', 'missing', 'result'}
        return {k if k in known else 'field_' + hashlib.sha256(k.encode()).hexdigest()[:8]: safe_value(v) for k, v in value.items()}
    return value
