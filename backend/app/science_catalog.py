"""Additive controller catalog. The G1 two-pure-tool registry remains intact."""
import threading,time
from pydantic import ValidationError
from .tools import tools,Tool,ToolResult
from .science_contracts import CONTRACTS,ArtifactOutput
from .science_artifacts import Artifacts
from .science_isolation import isolated,WorkerError
from .science_validation import validate_artifact
from .event_store import utc
from .workflow_models import digest


class ScienceCatalog:
    def __init__(self,root,audit):
        self.artifacts=Artifacts(root);self.audit=audit;self.capacity=threading.BoundedSemaphore(2)
        self._tools={**tools._tools,**{name:Tool(name,'1.0.0','G4_OFFLINE code-owned isolated UCI601 tool',schema,ArtifactOutput,None,10.) for name,schema in CONTRACTS.items()}}

    def discover(self):
        return tools.discover()+[{'name':name,'version':'1.0.0','description':'G4_OFFLINE isolated process, fixed UCI data only',
            'parameters':schema.model_json_schema(),'output':ArtifactOutput.model_json_schema(),'timeout_seconds':10.,
            'limits':{'memory_bytes':1024**3,'active_workers':2,'threads':1},'failure_types':['invalid_input','permission_denied','science_timeout','cancelled','science_worker_failed']}
            for name,schema in CONTRACTS.items()]

    def invoke(self,name,arguments,*,allowed_tools,enabled,context):
        if name not in CONTRACTS:return tools.invoke(name,arguments,allowed_tools=allowed_tools,enabled=enabled)
        if not enabled or name not in allowed_tools:return ToolResult(ok=False,error='permission_denied')
        try:params=CONTRACTS[name].model_validate(arguments).model_dump()
        except ValidationError:return ToolResult(ok=False,error='invalid_input')
        signal,deadline=context['signal'],context['deadline'];reserved=False
        try:
            while not reserved:
                if signal.is_set():return ToolResult(ok=False,error='cancelled')
                if time.monotonic()>=deadline:return ToolResult(ok=False,error='science_timeout')
                reserved=self.capacity.acquire(timeout=.01)
            inputs={}
            expected={'profile_id':'data_profiler','leakage_id':'leakage_check','training_id':'model_trainer',
                      'dummy_id':'model_evaluator','logistic_id':'model_evaluator','forest_id':'model_evaluator'}
            for key,kind in expected.items():
                if key not in params:continue
                record=self.artifacts.load(context['run_id'],params[key])
                if record['tool']!=kind:raise ValueError('artifact_source_type')
                # A forged or unverified leakage result cannot authorize training.
                if key=='leakage_id' and record['payload'].get('safe') is not True:raise ValueError('leakage_rejected')
                inputs[key]=record['payload']
            payload,execution=isolated({'operation':name,'params':params,'inputs':inputs},signal,deadline)
            output=self.artifacts.save(context['run_id'],context['node_id'],context['attempt_id'],name,arguments,payload,execution)
            self.audit.append(context['run_id'],'ScienceArtifactCreated',node_id=context['node_id'],attempt_id=context['attempt_id'],
                tool=name,artifact_id=output['artifact_id'],artifact_digest=output['artifact_digest'],execution=execution)
            return ToolResult(ok=True,output=output)
        except WorkerError as error:
            self.audit.append(context['run_id'],'ScienceProcessStopped',node_id=context['node_id'],attempt_id=context['attempt_id'],reason=error.code,execution=error.record)
            return ToolResult(ok=False,error=error.code)
        except (ValueError,KeyError,OSError):return ToolResult(ok=False,error='science_evidence_rejected')
        finally:
            if reserved:self.capacity.release()

    def verify(self,run_id,tool,arguments,output,policy,sensitive=False):
        if tool not in CONTRACTS:
            from .validators import validators
            return validators.verify(tool,arguments,output,policy,sensitive)
        code=None;recoverable=False
        try:
            ArtifactOutput.model_validate(output)
            record=self.artifacts.load(run_id,output['artifact_id'])
            if output['artifact_digest']!=record['artifact_digest']:
                code='artifact_digest_mismatch';recoverable=True
            else:validate_artifact(tool,arguments,record,self.artifacts,run_id)
        except (ValueError,KeyError,OSError,TypeError):code='science_hard_gate_failed'
        return [{'rule_id':'science_integrity_and_metrics','version':'1.0.0','passed':code is None,
                 'failure':{'code':code,'path':'$','recoverable':recoverable} if code else None,'difference':{},
                 'input_digest':digest(arguments),'output_digest':digest(output),'timestamp':utc()}]
