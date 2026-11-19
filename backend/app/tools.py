"""Code-owned, pure, bounded tools. No plugin loading or user code execution."""
import math
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import Field, ValidationError, field_validator

from .contracts import StrictModel


class Numbers(StrictModel):
    values: list[float] = Field(min_length=1, max_length=1000)

    @field_validator("values")
    @classmethod
    def finite(cls, values):
        if any(not math.isfinite(v) or abs(v) > 1e100 for v in values):
            raise ValueError("numbers must be finite and within +/-1e100")
        return values


class Summary(StrictModel):
    count: int
    minimum: float
    maximum: float
    mean: float


class Fields(StrictModel):
    # Flat scalar JSON only; bounded size, no recursive object traversal.
    document: dict[str, str | int | float | bool | None] = Field(max_length=100)
    required: list[str] = Field(min_length=1, max_length=100)

    @field_validator("required")
    @classmethod
    def names(cls, names):
        if any(not n or len(n) > 100 for n in names) or len(names) != len(set(names)):
            raise ValueError("required fields must be unique nonempty names up to 100 characters")
        return names

    @field_validator("document")
    @classmethod
    def size(cls, document):
        if any(len(k) > 100 or (isinstance(v, str) and len(v) > 2000) or
               (isinstance(v, float) and not math.isfinite(v)) for k, v in document.items()):
            raise ValueError("document scalar limits exceeded")
        return document


class FieldResult(StrictModel):
    valid: bool
    missing: list[str]


class ToolResult(StrictModel):
    ok: bool
    output: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    version: str
    description: str
    parameters: type[StrictModel]
    output: type[StrictModel]
    handler: Callable
    timeout_seconds: float = 1.0


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError("tool already registered")
        if not 0 < tool.timeout_seconds <= 5:
            raise ValueError("tool timeout must be within (0, 5]")
        self._tools[tool.name] = tool

    def names(self):
        return sorted(self._tools)

    def discover(self):
        return [{"name": t.name, "version": t.version, "description": t.description,
                 "parameters": t.parameters.model_json_schema(), "output": t.output.model_json_schema(),
                 "timeout_seconds": t.timeout_seconds,
                 "failure_types": ["unknown_tool", "permission_denied", "invalid_input", "timeout", "execution_error"]}
                for t in sorted(self._tools.values(), key=lambda t: t.name)]

    def invoke(self, name: str, arguments: dict, *, allowed_tools: list[str], enabled: bool) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, error="unknown_tool")
        if not enabled or name not in allowed_tools:
            return ToolResult(ok=False, error="permission_denied")
        try:
            params = tool.parameters.model_validate(arguments)
        except ValidationError:
            return ToolResult(ok=False, error="invalid_input")
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(tool.handler, params)
        try:
            value = future.result(timeout=tool.timeout_seconds)
            output = tool.output.model_validate(value).model_dump()
            return ToolResult(ok=True, output=output)
        except TimeoutError:
            return ToolResult(ok=False, error="timeout")
        except Exception:
            return ToolResult(ok=False, error="execution_error")
        finally:
            # Soft deadline only: threads cannot be killed. Only code-owned bounded
            # pure handlers are registered. Arbitrary plugins need process isolation.
            executor.shutdown(wait=False, cancel_futures=True)


def summarize(data: Numbers) -> Summary:
    return Summary(count=len(data.values), minimum=min(data.values), maximum=max(data.values),
                   mean=math.fsum(data.values) / len(data.values))


def check_fields(data: Fields) -> FieldResult:
    missing = sorted(set(data.required) - data.document.keys())
    return FieldResult(valid=not missing, missing=missing)


tools = ToolRegistry()
tools.register(Tool("numeric_summary", "1.0.0", "Bounded numeric summary, no I/O", Numbers, Summary, summarize))
tools.register(Tool("json_fields", "1.0.0", "Check required flat JSON fields, no I/O", Fields, FieldResult, check_fields))


def invoke_for_agent(registry, agent_id: str, name: str, arguments: dict) -> ToolResult:
    agent = registry.get(agent_id)
    return tools.invoke(name, arguments, allowed_tools=agent.allowed_tools, enabled=agent.enabled)
