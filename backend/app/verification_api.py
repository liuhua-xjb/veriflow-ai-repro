from fastapi import APIRouter, Request
from .workflow_models import RunCreate
from .verification_store import VerificationStore
from typing import Literal

router=APIRouter()


class TestRunCreate(RunCreate):
    fault: Literal['none','once_mean','permanent_mean','once_artifact_digest','permanent_artifact_digest'] = 'none'


def install(instance, test_only=False):
    @instance.get('/api/verification/config')
    def configuration():
        return {'version':'1.0.0','test_only_enabled':test_only,'external_llm':'NOT_EXECUTED','allowed_actions':['REEXECUTE_SAME_TOOL','STOP']}

    @instance.get('/api/runs/{run_id}/verification')
    def verification(run_id:str,request:Request):
        state=request.app.state
        run=state.orchestration.get(run_id)
        receipts=state.verification.receipts(run_id)
        return {'run_id':run_id,'status':run.get('verification_status','LEGACY_UNVERIFIED'),
                'nodes':{key:{k:v for k,v in node.items() if k in {'state','kind','verification_status','verification_reason','verification_evidence_id'}} for key,node in run['nodes'].items()},
                'receipts':receipts,'integrity':state.verification.integrity(run_id)}

    @instance.get('/api/runs/{run_id}/attempts')
    def attempts(run_id:str,request:Request):
        request.app.state.orchestration.get(run_id)
        return [r for r in request.app.state.verification.receipts(run_id) if r['kind'] in {'AttemptStarted','AttemptFinished','ValidationResult','FeedbackAction','VerificationStopped'}]

    if test_only:
        @instance.post('/api/test-only/runs',status_code=202)
        def test_run(value:TestRunCreate,request:Request):
            return request.app.state.controller.start(value,fault=value.fault)
