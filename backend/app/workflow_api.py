from fastapi import APIRouter, Query, Request

from .workflow_models import RunCreate, WorkflowCreate, WorkflowRevision, WorkflowSpec

router = APIRouter()


@router.get('/api/workflow-templates')
def templates():
    from .workflow_templates import templates, SAMPLE_INPUT
    return {'templates': templates(), 'sample_input': SAMPLE_INPUT}


@router.post('/api/workflows', status_code=201)
def create(value: WorkflowCreate, request: Request):
    return request.app.state.controller.save_workflow(value.workflow_id, WorkflowSpec.model_validate(value.model_dump(exclude={'workflow_id'})))


@router.get('/api/workflows')
def workflows(request: Request):
    return request.app.state.orchestration.workflows()


@router.get('/api/workflows/{workflow_id}')
def workflow(workflow_id: str, request: Request, version: int | None = Query(None, ge=1)):
    return request.app.state.orchestration.workflow(workflow_id, version)


@router.get('/api/workflows/{workflow_id}/versions')
def versions(workflow_id: str, request: Request):
    return request.app.state.orchestration.versions(workflow_id)


@router.post('/api/workflows/{workflow_id}/versions', status_code=201)
def revise(workflow_id: str, value: WorkflowRevision, request: Request):
    return request.app.state.controller.save_workflow(workflow_id, WorkflowSpec.model_validate(value.model_dump(exclude={'base_version'})), value.base_version)


@router.post('/api/runs', status_code=202)
def start(value: RunCreate, request: Request):
    return request.app.state.controller.start(value)


@router.get('/api/runs')
def runs(request: Request):
    return request.app.state.orchestration.runs()


@router.get('/api/runs/{run_id}')
def run(run_id: str, request: Request):
    return request.app.state.orchestration.get(run_id)


@router.get('/api/runs/{run_id}/events')
def events(run_id: str, request: Request, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
    values = request.app.state.orchestration.events(run_id, after, limit)
    return {'events': values, 'next_after': values[-1]['seq'] if values else after}


@router.post('/api/runs/{run_id}/cancel')
def cancel(run_id: str, request: Request):
    return request.app.state.controller.cancel(run_id)
