"""Static graph/type/permission checks and bounded flattening before execution."""
from dataclasses import dataclass, field
from pydantic import ValidationError

from .tools import tools
from .workflow_models import Binding, WorkflowSpec, compatible, fail, value_type

TOOL_TYPES = {'numeric_summary': ({'values': 'numbers'}, {'count': 'integer', 'minimum': 'number', 'maximum': 'number', 'mean': 'number'}),
              'json_fields': ({'document': 'object', 'required': 'strings'}, {'valid': 'boolean', 'missing': 'strings'})}


@dataclass
class CompiledNode:
    id: str
    kind: str
    spec: object
    deps: set[str] = field(default_factory=set)
    inputs: dict = field(default_factory=dict)
    predicate: dict | None = None
    gate: tuple | None = None
    parent: str | None = None


from .science_contracts import TOOL_TYPES as SCIENCE_TYPES
TOOL_TYPES.update(SCIENCE_TYPES)


def compile_workflow(spec, workflow_id, version, resolve, agents, catalog=None):
    catalog = catalog or tools
    flat = {}
    references = {}
    def expand(current, key, prefix='', arguments=None, inherited=None, stack=()):
        if key in stack: fail('recursive_subworkflow')
        if len(stack) >= 3: fail('subworkflow_depth_limit')
        stack = (*stack, key)
        by_id = {n.id: n for n in current.nodes}
        if len(by_id) != len(current.nodes): fail('duplicate_node')
        deps = {n.id: set() for n in current.nodes}
        pairs = set()
        for e in current.edges:
            if e.source not in by_id or e.target not in by_id: fail('dangling_edge')
            if (e.source, e.target) in pairs: fail('duplicate_edge')
            pairs.add((e.source, e.target)); deps[e.target].add(e.source)
        order = []
        pending = set(by_id)
        while pending:
            ready = sorted(n for n in pending if deps[n] <= set(order))
            if not ready: fail('dag_cycle')
            order.extend(ready); pending.difference_update(ready)
        ancestors = {}
        types = {}
        def binding_type(binding, allowed):
            if binding.source == 'literal': return value_type(binding.value)
            if binding.source == 'input':
                if binding.field not in current.input_schema: fail('undeclared_input')
                return current.input_schema[binding.field].type
            if binding.node_id not in allowed: fail('unmet_data_dependency')
            if binding.field == '$': return 'object'
            if binding.field not in types[binding.node_id]: fail('unknown_output_field')
            return types[binding.node_id][binding.field]
        def bind(b):
            if b.source == 'input' and arguments is not None: return arguments[b.field]
            if b.source == 'node': return {'source': 'node', 'node_id': prefix + b.node_id, 'field': b.field}
            return b.model_dump()
        for node_id in order:
            node = by_id[node_id]
            ancestors[node_id] = deps[node_id] | set().union(*(ancestors[d] for d in deps[node_id]))
            if node.gate:
                if node.gate.condition not in deps[node_id] or by_id[node.gate.condition].kind != 'condition': fail('invalid_branch_gate')
            params = {k: binding_type(b, ancestors[node_id]) for k, b in node.inputs.items()}
            if node.kind == 'tool_task':
                expected, output_types = TOOL_TYPES[node.tool]
                if set(params) != set(expected) or any(not compatible(params[k], expected[k]) for k in params): fail('tool_input_type_mismatch')
                if node.agent_id and not any(a.id == node.agent_id for a in agents): fail('unknown_agent')
                eligible = [a for a in agents if a.enabled and node.capability in a.capabilities and node.tool in a.allowed_tools and (not node.agent_id or a.id == node.agent_id)]
                if not eligible: fail('no_eligible_agent')
                if all(b.source == 'literal' for b in node.inputs.values()):
                    try:
                        catalog._tools[node.tool].parameters.model_validate({k: b.value for k, b in node.inputs.items()})
                    except ValidationError: fail('invalid_tool_literal')
                types[node_id] = output_types
            elif node.kind == 'condition':
                left_type = binding_type(node.predicate.left, ancestors[node_id])
                if node.predicate.op in {'gt', 'lt'} and (left_type not in {'integer', 'number'} or type(node.predicate.right) not in {int, float}): fail('predicate_type_mismatch')
                if node.predicate.op == 'eq' and not compatible(value_type(node.predicate.right), left_type): fail('predicate_type_mismatch')
                types[node_id] = {'result': 'boolean'}
            else: types[node_id] = params
            full = prefix + node_id
            full_deps = {prefix + d for d in deps[node_id]} or set(inherited or [])
            full_gate = (prefix + node.gate.condition, node.gate.equals) if node.gate else None
            if node.kind in {'bounded_loop', 'subworkflow'}:
                ref = (node.workflow.workflow_id, node.workflow.version)
                if ref in stack: fail('recursive_subworkflow')
                child = resolve(*ref)
                child = WorkflowSpec.model_validate({k: child[k] for k in WorkflowSpec.model_fields})
                if any(f.sensitive for f in child.input_schema.values()) and not any(f.sensitive for f in current.input_schema.values()):
                    fail('sensitive_child_requires_parent_annotation')
                references['%s@%s' % ref] = child.model_dump(mode='json')
                if set(params) != set(child.input_schema) or any(not compatible(params[k], child.input_schema[k].type) for k in params): fail('subworkflow_input_type_mismatch')
                start = full + '/start'
                flat[start] = CompiledNode(start, 'parallel', node, full_deps, gate=full_gate, parent=prefix.rstrip('/') or None)
                previous = {start}
                for iteration in range(node.iterations or 1):
                    child_prefix = full + '/' + str(iteration + 1) + '/'
                    exports = expand(child, ref, child_prefix, {k: bind(v) for k, v in node.inputs.items()}, previous, stack)
                    end = full + '/end_' + str(iteration + 1)
                    children = {child_prefix + n.id for n in child.nodes}
                    flat[end] = CompiledNode(end, 'join', node, children, exports[0], parent=full)
                    previous = {end}
                flat[full] = CompiledNode(full, 'join', node, previous, {k: {'source':'node','node_id':end,'field':k} for k in exports[1]}, parent=prefix.rstrip('/') or None)
                types[node_id] = exports[1]
            else:
                flat[full] = CompiledNode(full, node.kind, node, full_deps, {k: bind(v) for k, v in node.inputs.items()},
                    {**node.predicate.model_dump(), 'left': bind(node.predicate.left)} if node.predicate else None,
                    full_gate, prefix.rstrip('/') or None)
            if len(flat) > 100: fail('expanded_node_limit')
        result_types = {k: binding_type(v, set(order)) for k, v in current.outputs.items()}
        return {k: bind(v) for k, v in current.outputs.items()}, result_types
    exports, output_types = expand(spec, (workflow_id, version))
    return flat, exports, references
