from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints, field_validator

from .contracts import StrictModel
from .tools import tools

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class AgentSpec(StrictModel):
    name: Label
    role: Literal["planner", "analyst", "validator"]
    description: str = Field(default="", max_length=2000)
    capabilities: list[Identifier] = Field(min_length=1, max_length=32)
    allowed_tools: list[Identifier] = Field(default_factory=list, max_length=16)
    model_profile: Literal["offline", "default"] = "offline"
    memory_policy: Literal["none", "session"] = "none"
    concurrency_limit: int = Field(default=1, ge=1, le=16)
    enabled: bool = True

    @field_validator("capabilities", "allowed_tools")
    @classmethod
    def unique(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("duplicate capability or tool")
        return values

    @field_validator("allowed_tools")
    @classmethod
    def registered(cls, values):
        from .science_contracts import CONTRACTS
        if any(name not in set(tools.names()) | set(CONTRACTS) for name in values):
            raise ValueError("unknown tool; see GET /api/tools")
        return values


class AgentCreate(AgentSpec):
    id: Identifier = Field(default_factory=lambda: "agent_" + uuid4().hex)
    version: Literal[1] = 1

    @field_validator("version", mode="before")
    @classmethod
    def initial_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("initial version must be integer 1")
        return value


class AgentVersion(AgentSpec):
    base_version: int = Field(ge=1)


class AgentEnabled(StrictModel):
    enabled: bool
    base_version: int = Field(ge=1)


class AgentRecord(AgentSpec):
    id: Identifier
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


TEMPLATES = [AgentSpec(name="规划 Agent", role="planner", description="规划角色定义，尚未执行任务",
                       capabilities=["planning"]),
             AgentSpec(name="分析 Agent", role="analyst", description="分析角色定义，尚未执行任务",
                       capabilities=["analysis"], allowed_tools=["numeric_summary"]),
             AgentSpec(name="验证 Agent", role="validator", description="验证角色定义，尚未实现 G3 验证流程",
                       capabilities=["field_check"], allowed_tools=["json_fields"])]
