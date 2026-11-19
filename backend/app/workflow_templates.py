"""Small synthetic declarative samples. These are definitions, never fake results."""
def inp(name): return {'source': 'input', 'field': name}
def out(node, name): return {'source': 'node', 'node_id': node, 'field': name}
def literal(value): return {'source': 'literal', 'value': value}
def edge(source, target): return {'source': source, 'target': target}


def stat(node_id, delay=0, **extra):
    return {'id': node_id, 'kind': 'tool_task', 'tool': 'numeric_summary', 'capability': 'analysis',
            'inputs': {'values': inp('numbers')}, 'service_delay_ms': delay, **extra}


def field_check(node_id, document=None, **extra):
    return {'id': node_id, 'kind': 'tool_task', 'tool': 'json_fields', 'capability': 'analysis',
            'inputs': {'document': document or inp('document'), 'required': literal(['mean'])}, **extra}


def templates():
    schema = {'numbers': {'type': 'numbers'}, 'document': {'type': 'object'}}
    condition = {'id': 'decide', 'kind': 'condition', 'predicate': {'left': out('summary', 'mean'), 'op': 'gt', 'right': 3.0}}
    values = [
        {'workflow_id':'g2_serial', 'description':'Synthetic serial: summary -> condition -> field check', 'input_schema':schema,
         'nodes':[stat('summary', 100), condition, field_check('check', out('summary','$'), gate={'condition':'decide','equals':True})],
         'edges':[edge('summary','decide'),edge('decide','check')]},
        {'workflow_id':'g2_parallel', 'description':'Synthetic parallel: two summaries -> join -> consume', 'input_schema':schema,
         'nodes':[{'id':'fork','kind':'parallel'}, stat('left',200), stat('right',200),
                  {'id':'merge','kind':'join','inputs':{'mean':out('left','mean'), 'other':out('right','mean')}}, field_check('consume',out('merge','$'))],
         'edges':[edge('fork','left'),edge('fork','right'),edge('left','merge'),edge('right','merge'),edge('merge','consume')]},
        {'workflow_id':'g2_condition','description':'Synthetic condition: true/false with explicit SKIPPED path','input_schema':schema,
         'nodes':[stat('summary',100), condition,
                  field_check('positive', gate={'condition':'decide','equals':True}),
                  field_check('negative', gate={'condition':'decide','equals':False})],
         'edges':[edge('summary','decide'),edge('decide','positive'),edge('decide','negative')]},
        {'workflow_id':'g2_body','description':'Pinned child body','input_schema':schema,
         'nodes':[stat('summary',100)],'outputs':{'mean':out('summary','mean')}},
        {'workflow_id':'g2_loop','description':'Three bounded repetitions; no repair or retry','input_schema':schema,
         'nodes':[{'id':'repeat','kind':'bounded_loop','workflow':{'workflow_id':'g2_body','version':1},'iterations':3,
                   'inputs':{'numbers':inp('numbers'),'document':inp('document')}}], 'outputs':{'mean':out('repeat','mean')}},
        {'workflow_id':'g2_subworkflow','description':'Pinned child workflow with parent-child event paths','input_schema':schema,
         'nodes':[{'id':'child','kind':'subworkflow','workflow':{'workflow_id':'g2_body','version':1},
                   'inputs':{'numbers':inp('numbers'),'document':inp('document')}}, field_check('check',out('child','$'))],
         'edges':[edge('child','check')]},
    ]
    return values


SAMPLE_INPUT = {'numbers':[2.0,4.0,6.0], 'document':{'mean':4.0}}
