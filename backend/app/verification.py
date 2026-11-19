"""Bounded same-tool re-execution. No input mutation, model call, or rule waiver."""
import threading
import time
from .event_store import utc
from .validators import validators, fingerprint, preview
from .tools import tools


class VerificationSession:
    def __init__(self, run_id, policy, audit, events, timeout, sensitive, fault='none', catalog=None):
        self.catalog=catalog or tools
        self.run_id,self.policy,self.audit,self.events=run_id,policy,audit,events
        self.deadline=time.monotonic()+min(timeout,policy.feedback.deadline_seconds)
        self.sensitive,self.fault=sensitive,fault
        self.lock=threading.RLock()
        self.attempts=0
        audit.append(run_id,'VerificationConfigured',policy=policy.model_dump(),test_label='TEST_ONLY' if fault!='none' else None,
                     llm_inference='NOT_EXECUTED',tokens=0,cost=0)

    def budget(self):
        with self.lock:
            return {'attempts_used':self.attempts,'attempts_remaining':max(0,self.policy.feedback.max_total_attempts-self.attempts),
                    'deadline_remaining_seconds':max(0,self.deadline-time.monotonic()),'tokens':0,'cost':0}

    def mark(self,node,status,receipt=None,reason=None):
        fields={'verification_status':status}
        if receipt: fields['verification_evidence_id']=receipt['evidence_id']
        if reason: fields['verification_reason']=reason
        self.events.update(self.run_id,'Verification'+status,node.id,fields=fields,
                           metadata={'evidence_id':receipt['evidence_id'] if receipt else None,'reason':reason})

    def execute(self,node,arguments,agent,signal,invoke):
        begin=utc(); feedback=self.policy.feedback; failures=[]
        def reject(reason, receipt=None):
            if receipt is None:
                receipt=self.audit.append(self.run_id,'VerificationStopped',node_id=node.id,status='REJECTED',reason=reason,budget=self.budget())
            self.mark(node,'REJECTED',receipt,reason)
            return None,reason,begin,utc()
        for number in range(1,feedback.max_repairs+2):
            with self.lock:
                if signal.is_set(): return reject('cancelled')
                if time.monotonic()>=self.deadline: return reject('verification_deadline')
                if self.attempts>=feedback.max_total_attempts: return reject('attempt_budget_exhausted')
                self.attempts+=1
            attempt_id=f'{node.id}:{number}'
            start=self.audit.append(self.run_id,'AttemptStarted',node_id=node.id,attempt_id=attempt_id,
                tool=node.spec.tool,tool_version=self.catalog._tools[node.spec.tool].version,agent_id=agent.id,agent_version=agent.version,
                workflow_version=self.events.get(self.run_id)['workflow_version'],validator_version=validators.version,
                input_digest=fingerprint(arguments),budget=self.budget(),test_label='TEST_ONLY' if self.fault!='none' else None)
            self.events.update(self.run_id,'ToolAttemptStarted',node.id,fields={'attempt_id':attempt_id},metadata={'attempt_id':attempt_id,'evidence_id':start['evidence_id']})
            value,error,_,end=invoke()
            source_digest=fingerprint(value)
            # Only installed on the explicit test server. The actual G1 tool ran first.
            injected=False
            if not error and node.spec.tool=='numeric_summary' and (self.fault=='permanent_mean' or self.fault=='once_mean' and number==1):
                value={**value,'mean':value['mean']+1.0};injected=True
            if not error and node.spec.tool=='model_evaluator' and (self.fault=='permanent_artifact_digest' or self.fault=='once_artifact_digest' and number==1):
                value={**value,'artifact_digest':'0'*64};injected=True
            self.mark(node,'EXECUTED')
            if signal.is_set(): error='cancelled'
            elif time.monotonic()>=self.deadline: error='verification_deadline'
            if error:
                receipt=self.audit.append(self.run_id,'AttemptFinished',node_id=node.id,attempt_id=attempt_id,
                    status='REJECTED',reason=error,input_digest=fingerprint(arguments),output_digest=fingerprint(value),
                    source_output_digest=source_digest,output_preview=preview(value,self.sensitive),actual_tool_completed=False,budget=self.budget())
                return reject(error,receipt)
            if self.policy.mode=='execution_only':
                receipt=self.audit.append(self.run_id,'AttemptFinished',node_id=node.id,attempt_id=attempt_id,status='UNVERIFIED',reason='execution_only',
                    output_digest=fingerprint(value),source_output_digest=source_digest,output_preview=preview(value,self.sensitive),
                    actual_tool_completed=True,test_label='TEST_ONLY' if injected else None,budget=self.budget())
                self.mark(node,'UNVERIFIED',receipt)
                return value,None,begin,end
            self.mark(node,'VERIFYING')
            results=self.catalog.verify(self.run_id,node.spec.tool,arguments,value,self.policy,self.sensitive) if hasattr(self.catalog,'verify') else validators.verify(node.spec.tool,arguments,value,self.policy,self.sensitive)
            for result in results:
                self.audit.append(self.run_id,'ValidationResult',node_id=node.id,attempt_id=attempt_id,result=result)
            failed=[r for r in results if not r['passed']]
            status='REJECTED' if failed else 'VERIFIED'
            if signal.is_set() or time.monotonic()>=self.deadline:
                status='REJECTED';error='cancelled' if signal.is_set() else 'verification_deadline'
            receipt=self.audit.append(self.run_id,'AttemptFinished',node_id=node.id,attempt_id=attempt_id,status=status,
                reason=error or ('hard_gate_failed' if failed else 'all_applicable_rules_passed'),
                input_digest=fingerprint(arguments),output_digest=fingerprint(value),source_output_digest=source_digest,
                output_preview=preview(value,self.sensitive),actual_tool_completed=True,test_label='TEST_ONLY' if injected else None,budget=self.budget())
            if error: return reject(error,receipt)
            if not failed:
                self.mark(node,'VERIFIED',receipt)
                return value,None,begin,utc()
            signature=fingerprint([r['failure'] for r in failed]);failures.append(signature)
            if any(not r['failure']['recoverable'] for r in failed): reason='unrecoverable_evidence_missing'
            elif self.policy.mode!='verify_and_repair': reason='verification_rejected'
            elif number>feedback.max_repairs: reason='max_repairs_exhausted'
            elif failures.count(signature)>=feedback.no_progress_limit: reason='no_progress'
            elif self.budget()['attempts_remaining']==0: reason='attempt_budget_exhausted'
            else: reason=None
            action=self.audit.append(self.run_id,'FeedbackAction',node_id=node.id,attempt_id=attempt_id,
                action='STOP' if reason else 'REEXECUTE_SAME_TOOL',reason=reason or 'recoverable_hard_gate_failure',
                error_fingerprint=signature,previous_evidence_id=receipt['evidence_id'],repair_index=number if not reason else number-1,
                repairs_remaining=max(0,feedback.max_repairs-number+1),budget=self.budget(),input_unchanged=True)
            self.events.update(self.run_id,'FeedbackAction',node.id,metadata={'attempt_id':attempt_id,'evidence_id':action['evidence_id'],'reason':reason,'action':action['action']})
            if reason: return reject(reason,action)
        return reject('max_repairs_exhausted')

    def finish(self,state,error):
        run=self.events.get(self.run_id)
        nodes=[n for n in run['nodes'].values() if n['kind']=='tool_task' and n['state']!='SKIPPED']
        status=('VERIFIED' if nodes and all(n.get('verification_status')=='VERIFIED' for n in nodes) else 'UNVERIFIED') if state=='SUCCEEDED' else 'REJECTED'
        receipt=self.audit.append(self.run_id,'RunVerificationFinished',status=status,reason=error or self.policy.mode,budget=self.budget(),tokens=0,cost=0)
        return {'verification_status':status,'verification_evidence_id':receipt['evidence_id'],'verification_budget':self.budget()}
