"""Bounded controller with orthogonal deterministic verification and audit."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .agents import AgentRecord
from .event_store import EventStore, TERMINAL, utc
from .registry import RegistryError
from .process_lease import ProcessLease
from .scheduler import Scheduler
from .tools import tools
from .verification import VerificationSession
from .workflow_compile import compile_workflow
from .workflow_models import WorkflowSpec, compatible, digest, fail, safe_value, value_type


class RunController:
    def __init__(self, store: EventStore, agents, verification=None):
        from .verification_store import VerificationStore
        verification = verification or VerificationStore(store.path.with_name('verification.sqlite3'))
        self.verification = verification
        from .science_catalog import ScienceCatalog
        self.catalog = ScienceCatalog(store.path.parent/'science', verification)
        self.store, self.agents = store, agents
        self.lease = ProcessLease(store.path.with_suffix('.owner.lock'))
        self.scheduler = Scheduler()
        self.lock = threading.RLock()
        self.jobs = {}
        self.controllers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='vf-run')
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='vf-tool')
        self.closed = False
        store.interrupt_orphans()
        verification.recover(store)

    def validate_workflow(self, workflow_id, version, spec, agents=None):
        return compile_workflow(spec, workflow_id, version, self.store.workflow, agents or self.agents.list(), catalog=self.catalog)

    def save_workflow(self, workflow_id, spec, base_version=None):
        self.validate_workflow(workflow_id, (base_version or 0) + 1, spec)
        return self.store.save_workflow(workflow_id, spec, base_version)

    def start(self, request, fault='none'):
        with self.lock:
            if self.closed: raise RegistryError(503, 'controller_closed')
            self.jobs = {key:value for key,value in self.jobs.items() if not value[1].done()}
            record = self.store.workflow(request.workflow_id, request.workflow_version)
            spec = WorkflowSpec.model_validate({k: record[k] for k in WorkflowSpec.model_fields})
            # Materialized Agent objects and explicit workflow versions form the run snapshot.
            agents = [AgentRecord.model_validate_json(a.model_dump_json()) for a in self.agents.list()]
            flat, exports, references = self.validate_workflow(request.workflow_id, request.workflow_version, spec, agents)
            try:
                if set(request.inputs) != set(spec.input_schema): fail('input_fields_mismatch')
                if any(not compatible(value_type(v), spec.input_schema[k].type) for k, v in request.inputs.items()): fail('run_input_type_mismatch')
            except (ValueError, ValidationError): fail('invalid_run_input')
            run_id = 'run_' + uuid4().hex
            request_identity = {**request.model_dump(exclude={'idempotency_key'}), 'test_fault':fault}
            previous = self.verification.claim(request.idempotency_key, request_identity, run_id, reserve=False)
            if previous:
                try: return self.store.get(previous)
                except RegistryError: raise RegistryError(409, 'idempotency_incomplete')
            if sum(not f.done() for _, f in self.jobs.values()) >= 4: raise RegistryError(409, 'run_capacity_limit')
            self.verification.claim(request.idempotency_key, request_identity, run_id)
            nodes = {key: {'state': 'PENDING', 'kind': n.kind, 'dependencies': sorted(n.deps),
                           'parent_node_id': n.parent, 'attempt_id': key + ':1',
                           'gate': list(n.gate) if n.gate else None} for key, n in flat.items()}
            run = {'run_id': run_id, 'workflow_id': request.workflow_id, 'workflow_version': request.workflow_version,
                   'state': 'PENDING', 'created_at': utc(), 'execution_mode': 'deterministic_tool_only',
                   'llm_inference': 'NOT_EXECUTED', 'strategy': request.strategy,
                   'verification_status':'UNVERIFIED', 'verification_policy':request.verification.model_dump(), 'test_label':'TEST_ONLY' if fault!='none' else None,
                   'scheduler_config': request.scheduler.model_dump(), 'scheduler_config_digest': digest(request.scheduler.model_dump()), 'input_digest': digest(request.inputs),
                   'workflow_snapshot': record, 'subworkflow_snapshots': references,
                   'agent_snapshots': [{k: a.model_dump(mode='json')[k] for k in ('id', 'version', 'capabilities', 'allowed_tools', 'enabled', 'concurrency_limit', 'model_profile')} for a in agents],
                   'tool_versions': {m['name']: m['version'] for m in self.catalog.discover()}, 'nodes': nodes}
            self.store.create_run(run)
            session = VerificationSession(run_id, request.verification, self.verification, self.store, spec.timeout_seconds, any(f.sensitive for f in spec.input_schema.values()), fault, self.catalog)
            signal = threading.Event()
            future = self.controllers.submit(self._run, run_id, request, spec, flat, exports, agents, signal, session)
            self.jobs[run_id] = (signal, future)
            return self.store.get(run_id)

    def cancel(self, run_id):
        with self.lock:
            run = self.store.get(run_id)
            if run['state'] in TERMINAL: raise RegistryError(409, 'run_already_terminal')
            if run_id not in self.jobs: raise RegistryError(409, 'use_coordinator_cancel_endpoint')
            signal, _ = self.jobs[run_id]
            signal.set()
            self.store.update(run_id, 'CancelRequested')
        return self.store.get(run_id)

    @staticmethod
    def resolve(binding, inputs, outputs):
        source = binding['source']
        if source == 'literal': return binding.get('value')
        if source == 'input': return inputs[binding['field']]
        value = outputs[binding['node_id']]
        return value if binding['field'] == '$' else value[binding['field']]

    def _tool(self, node, arguments, agent, signal, activity, session):
        try:
            return session.execute(node, arguments, agent, signal, lambda: self._tool_once(node, arguments, agent, signal, activity, session, node.id))
        finally:
            self.scheduler.release(agent)

    def _tool_once(self, node, arguments, agent, signal, activity, session, node_id):
        deadline = session.deadline
        start = time.monotonic(); started_at = utc()
        with self.lock:
            activity['current'] += 1
            activity['peak'] = max(activity['peak'], activity['current'])
        try:
            # Deliberate synthetic service occupancy, configured and reported equally
            # for both strategies. Never reported as measured native tool latency.
            service_deadline = start + node.spec.service_delay_ms / 1000
            remaining = service_deadline - time.monotonic()
            while remaining > 0:
                if time.monotonic() >= deadline: return None, 'verification_deadline', started_at, utc()
                if signal.wait(min(.005, remaining)): return None, 'cancelled', started_at, utc()
                remaining = service_deadline - time.monotonic()
                if time.monotonic() - start >= node.spec.timeout_seconds: return None, 'node_timeout', started_at, utc()
            if signal.is_set(): return None, 'cancelled', started_at, utc()
            result = self.catalog.invoke(node.spec.tool, arguments, allowed_tools=agent.allowed_tools, enabled=agent.enabled,
                context={'run_id':session.run_id,'node_id':node_id,'attempt_id':self.store.get(session.run_id)['nodes'][node_id]['attempt_id'],
                         'signal':signal,'deadline':min(deadline,start+node.spec.timeout_seconds)})
            if time.monotonic() - start > node.spec.timeout_seconds: return None, 'node_timeout', started_at, utc()
            return result.output, None if result.ok else result.error, started_at, utc()
        finally:
            with self.lock: activity['current'] -= 1

    def _run(self, run_id, request, spec, flat, exports, agents, signal, session):
        started = time.monotonic()
        states = {key: 'PENDING' for key in flat}
        outputs, active, ready_ticks = {}, {}, {}
        deferred = {}
        tick = 0; scheduling_ms = 0.0; max_concurrency = 0; distribution = {}
        activity = {'current':0, 'peak':0}
        error = None; terminal = 'SUCCEEDED'
        sensitive = any(f.sensitive for f in spec.input_schema.values())
        self.store.update(run_id, 'RunStarted', state='RUNNING', fields={'started_at': utc()})
        def change(key, state, fields=None, metadata=None):
            states[key] = state
            self.store.update(run_id, 'Node' + state.title(), key, state, fields, metadata)
        try:
            while not all(v in TERMINAL for v in states.values()):
                if signal.is_set(): terminal, error = 'CANCELLED', 'cancel_requested'; break
                if time.monotonic() - started >= spec.timeout_seconds: terminal, error = 'FAILED', 'run_timeout'; break
                for future, (key, agent) in list(active.items()):
                    if not future.done(): continue
                    del active[future]
                    value, failure, begin, end = future.result()
                    if failure:
                        change(key, 'FAILED', {'error': failure, 'worker_started_at': begin, 'finished_at': end})
                        terminal, error = 'FAILED', failure
                        break
                    outputs[key] = value
                    change(key, 'SUCCEEDED', {'result': '[REDACTED]' if sensitive else safe_value(value), 'output_digest': digest(value),
                                             'worker_started_at': begin, 'finished_at': end})
                if error: break
                candidates = sorted(flat, key=lambda key: (-(request.scheduler.priority_weight * flat[key].spec.priority +
                    request.scheduler.age_weight * (tick - ready_ticks.get(key, tick))), key) if request.strategy == 'dynamic' else (0, key))
                for key in candidates:
                    node = flat[key]
                    if states[key] in TERMINAL or states[key] == 'RUNNING': continue
                    if not all(states[d] in TERMINAL for d in node.deps): continue
                    if (node.kind != 'join' and any(states[d] == 'SKIPPED' for d in node.deps)) or (node.kind == 'join' and node.deps and all(states[d] == 'SKIPPED' for d in node.deps)):
                        change(key, 'SKIPPED', {'reason': 'upstream_branch_skipped'}); continue
                    if node.gate and outputs.get(node.gate[0], {}).get('result') != node.gate[1]:
                        change(key, 'SKIPPED', {'reason': 'branch_not_selected'}); continue
                    if states[key] == 'PENDING':
                        change(key, 'READY'); ready_ticks[key] = tick
                    if request.strategy == 'fixed_serial' and active: continue
                    if len(active) >= spec.max_concurrency: continue
                    agent = None
                    if node.kind == 'tool_task':
                        before = time.perf_counter()
                        chosen, trace = self.scheduler.choose(node.spec, agents, request.strategy, request.scheduler, tick - ready_ticks[key], reserve=True)
                        scheduling_ms += (time.perf_counter() - before) * 1000
                        if chosen is None:
                            signature = digest(trace)
                            if deferred.get(key) != signature:
                                self.store.update(run_id, 'SchedulerDeferred', key, metadata={'candidates':trace,
                                    'reason':'capacity_busy', 'strategy':request.strategy, 'config_version':request.scheduler.version})
                                deferred[key] = signature
                            continue
                        agent = next(a for a in agents if a.id == chosen['agent_id'])
                        distribution[agent.id] = distribution.get(agent.id, 0) + 1
                        self.store.update(run_id, 'AgentAssigned', key, metadata={'selected': chosen, 'candidates': trace,
                            'strategy': request.strategy, 'config_version': request.scheduler.version,
                            'config_digest': digest(request.scheduler.model_dump())})
                    try:
                        arguments = {k: self.resolve(b, request.inputs, outputs) for k, b in node.inputs.items()}
                        if node.kind == 'condition':
                            arguments = {'left': self.resolve(node.predicate['left'], request.inputs, outputs),
                                         'right': node.predicate['right']}
                    except (KeyError, TypeError):
                        if agent: self.scheduler.release(agent)
                        change(key, 'FAILED', {'error': 'input_unavailable'})
                        terminal, error = 'FAILED', 'input_unavailable'; break
                    fields = {'started_at': utc(), 'input_digest': digest(arguments), 'input_preview': '[REDACTED]' if sensitive else safe_value(arguments)}
                    if agent: fields.update(agent_id=agent.id, agent_version=agent.version, tool=node.spec.tool,
                                            tool_version=self.catalog._tools[node.spec.tool].version, synthetic_service_delay_ms=node.spec.service_delay_ms)
                    change(key, 'RUNNING', fields)
                    tick += 1
                    if agent:
                        active[self.workers.submit(self._tool, node, arguments, agent, signal, activity, session)] = (key, agent)
                        max_concurrency = max(max_concurrency, len(active))
                    else:
                        if node.kind == 'condition':
                            left = arguments['left']
                            op, right = node.predicate['op'], arguments['right']
                            result = {'result': left is not None if op == 'exists' else left == right if op == 'eq' else left > right if op == 'gt' else left < right}
                        else: result = arguments
                        outputs[key] = result
                        change(key, 'SUCCEEDED', {'result': '[REDACTED]' if sensitive else safe_value(result), 'output_digest': digest(result), 'finished_at': utc()})
                if error: break
                if active or not all(s in TERMINAL for s in states.values()): time.sleep(.002)
        except Exception:
            terminal, error = 'FAILED', 'execution_error'
        finally:
            signal.set()
            wait(active, timeout=2)
            for key, state in states.items():
                if state not in TERMINAL:
                    change(key, 'CANCELLED' if terminal == 'CANCELLED' or state == 'RUNNING' else 'SKIPPED', {'reason': error or 'terminated'})
            result = None
            if not error:
                try: result = {k: self.resolve(b, request.inputs, outputs) for k, b in exports.items()}
                except KeyError: terminal, error = 'FAILED', 'output_unavailable'
            verification_fields = session.finish(terminal, error)
            self.store.update(run_id, 'RunFinished', state=terminal, fields={**verification_fields, 'error': error, 'finished_at': utc(),
                'result': '[REDACTED]' if sensitive else safe_value(result), 'metrics': {'wall_ms': (time.monotonic()-started)*1000,
                'scheduling_ms': scheduling_ms, 'max_concurrency': activity['peak'], 'agent_distribution': distribution,
                'tool_tasks': sum(n.kind == 'tool_task' for n in flat.values()), 'error_count': int(error is not None)}})

    def close(self):
        with self.lock:
            self.closed = True
            for signal, future in self.jobs.values():
                if not future.done(): signal.set()
        self.controllers.shutdown(wait=True)
        self.workers.shutdown(wait=True)
        self.lease.close()
