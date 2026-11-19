from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .agents import AgentCreate, AgentEnabled, AgentRecord, AgentSpec, AgentVersion, TEMPLATES
from .registry import AgentRegistry, RegistryError
from .tools import tools
from .event_store import EventStore
from .runner import RunController
from .workflow_api import router as workflow_router

ROOT = Path(__file__).resolve().parents[2]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: Literal["veriflow-api"]
    version: Literal["0.1.0"]


def create_app(db_path: Path | None = None, *, test_only: bool = False, approval_session=None, g4r2_session=None, g4r3_session=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(instance):
        instance.state.trial_approval = approval_session
        instance.state.g4r2_approval = g4r2_session
        instance.state.g4r3_approval = g4r3_session
        instance.state.registry = AgentRegistry(db_path or ROOT / "runtime" / "agents.sqlite3")
        instance.state.orchestration = EventStore((db_path.parent if db_path else ROOT / 'runtime') / 'orchestration.sqlite3')
        from .verification_store import VerificationStore
        instance.state.verification = VerificationStore(instance.state.orchestration.path.with_name('verification.sqlite3'))
        instance.state.controller = RunController(instance.state.orchestration, instance.state.registry, instance.state.verification)
        from .deepseek_trial import attach_real_service
        attach_real_service(instance.state)
        from .g4r2_api import attach
        attach(instance.state,g4r2_session)
        from .g4r3_api import attach as attach_g4r3
        attach_g4r3(instance.state,g4r3_session)
        try:
            yield
        finally:
            instance.state.controller.close()
            if approval_session is not None: approval_session.close()
            if g4r2_session is not None: g4r2_session.close()
            if g4r3_session is not None: g4r3_session.close()

    instance = FastAPI(title="VeriFlow AI · 智流", version="0.2.0", lifespan=lifespan)
    instance.include_router(workflow_router)
    from .science_api import router as science_router
    instance.include_router(science_router)
    from .deepseek_api import router as deepseek_router
    instance.include_router(deepseek_router)
    from .deepseek_trial import router as trial_router
    instance.include_router(trial_router)
    from .g4r2_api import router as g4r2_router
    instance.include_router(g4r2_router)
    from .g4r3_api import router as g4r3_router
    instance.include_router(g4r3_router)
    from .verification_api import install
    install(instance, test_only)
    from .deepseek_approval import TrialApprovalMiddleware
    instance.add_middleware(TrialApprovalMiddleware)
    from .g4r2_approval import R2ApprovalMiddleware
    instance.add_middleware(R2ApprovalMiddleware)
    from .g4r3_approval import R3ApprovalMiddleware
    instance.add_middleware(R3ApprovalMiddleware)
    instance.add_middleware(CORSMiddleware,
                            allow_origins=["http://127.0.0.1:15173", "http://localhost:15173"],
                            allow_methods=["GET", "POST", "PATCH"], allow_headers=["Content-Type", "X-Veriflow-Trial-Approval", "X-Veriflow-G4R2-Approval", "X-Veriflow-G4R3-Approval"],
                            allow_credentials=False)

    @instance.exception_handler(RegistryError)
    async def registry_error(request: Request, error: RegistryError):
        return JSONResponse(status_code=error.status, content={"error": {"code": error.code}})

    @instance.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError):
        malformed = any(item["type"] == "json_invalid" for item in error.errors())
        known = set(AgentCreate.model_fields) | {"base_version", "version", "capability", "tool"}
        return JSONResponse(status_code=400 if malformed else 422, content={"error": {
            "code": "invalid_json" if malformed else "validation_error",
            "details": [{"type": item["type"],
                         "field": next((p for p in item["loc"] if isinstance(p, str) and p in known), "unknown"),
                         "message": item["msg"] if item["type"] != "json_invalid" else "Malformed JSON"}
                        for item in error.errors()]}})

    @instance.get("/api/health", response_model=HealthResponse)
    def health():
        return HealthResponse(status="ok", service="veriflow-api", version="0.1.0")

    @instance.get("/api/tools")
    def tool_list():
        return tools.discover()

    @instance.get("/api/agent-templates", response_model=list[AgentSpec])
    def templates():
        return TEMPLATES

    @instance.post("/api/agents", status_code=201, response_model=AgentRecord)
    def create_agent(value: AgentCreate, request: Request):
        return request.app.state.registry.create(value)

    @instance.get("/api/agents", response_model=list[AgentRecord])
    def list_agents(request: Request, capability: str | None = Query(None, max_length=64),
                    enabled: bool | None = None, tool: str | None = Query(None, max_length=64)):
        return request.app.state.registry.list(capability, enabled, tool)

    @instance.get("/api/agents/discover")
    def discover(request: Request, capability: str = Query(min_length=1, max_length=64),
                 tool: str | None = Query(None, max_length=64)):
        return [{"agent": a.model_dump(mode="json"), "reason": {"enabled": True,
                 "capability": capability, "tool": tool}}
                for a in request.app.state.registry.list(capability, True, tool)]

    @instance.get("/api/agents/{agent_id}", response_model=AgentRecord)
    def get_agent(agent_id: str, request: Request, version: int | None = Query(None, ge=1)):
        return request.app.state.registry.get(agent_id, version)

    @instance.get("/api/agents/{agent_id}/versions", response_model=list[AgentRecord])
    def versions(agent_id: str, request: Request):
        return request.app.state.registry.versions(agent_id)

    @instance.post("/api/agents/{agent_id}/versions", status_code=201, response_model=AgentRecord)
    def revise(agent_id: str, value: AgentVersion, request: Request):
        return request.app.state.registry.revise(agent_id, value.base_version,
            spec=AgentSpec.model_validate(value.model_dump(exclude={"base_version"})))

    @instance.patch("/api/agents/{agent_id}/enabled", response_model=AgentRecord)
    def enable(agent_id: str, value: AgentEnabled, request: Request):
        return request.app.state.registry.revise(agent_id, value.base_version, enabled=value.enabled)

    return instance


app = create_app()
