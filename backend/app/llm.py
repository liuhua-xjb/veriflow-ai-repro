"""Offline by default. No API route invokes this adapter. No retries/fake fallback."""
import json
import os
from abc import ABC, abstractmethod
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, ValidationError, field_validator

from .contracts import StrictModel


class ProviderError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact(k, secret): redact(v, secret) for k, v in value.items()}
    return value


class LLMConfig(StrictModel):
    enabled: bool = False
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    base_url: str = "http://127.0.0.1:18999/v1"
    model: str = Field(default="", max_length=100)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError("base URL must not contain credentials, query or fragment")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError("use HTTPS, except loopback simulated servers")
        return value.rstrip("/")

    @classmethod
    def from_env(cls):
        try:
            switch = os.environ.get("VERIFLOW_LLM_ENABLED", "false")
            if switch not in {"true", "false"}:
                raise ValueError()
            return cls(enabled=switch == "true", api_key=SecretStr(os.environ.get("VERIFLOW_LLM_API_KEY", "")),
                       base_url=os.environ.get("VERIFLOW_LLM_BASE_URL", "http://127.0.0.1:18999/v1"),
                       model=os.environ.get("VERIFLOW_LLM_MODEL", ""),
                       timeout_seconds=float(os.environ.get("VERIFLOW_LLM_TIMEOUT", "10")))
        except ValueError:
            raise ProviderError("invalid_configuration") from None


class Message(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class FunctionDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(max_length=1000)
    parameters: dict[str, Any]

    @field_validator("parameters")
    @classmethod
    def bounded_schema(cls, value):
        if value.get("type") != "object" or len(json.dumps(value)) > 16384:
            raise ValueError("tool parameters require bounded object JSON schema")
        return value


class ProtocolTool(StrictModel):
    type: Literal["function"] = "function"
    function: FunctionDefinition


class FunctionCall(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: str = Field(max_length=8000)

    @field_validator("arguments")
    @classmethod
    def json_object(cls, value):
        if not isinstance(json.loads(value), dict):
            raise ValueError("tool call arguments must encode a JSON object")
        return value


class ToolCall(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    type: Literal["function"]
    function: FunctionCall


class ChatRequest(StrictModel):
    messages: list[Message] = Field(min_length=1, max_length=32)
    tools: list[ProtocolTool] | None = Field(default=None, max_length=8)


class ChatReply(StrictModel):
    content: str | None
    model: str
    finish_reason: Literal["stop", "length", "tool_calls"]
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=8)


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, request: ChatRequest) -> ChatReply: ...


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, config: LLMConfig):
        self.config = config

    def complete(self, request: ChatRequest) -> ChatReply:
        if not self.config.enabled:
            raise ProviderError("offline")
        key = self.config.api_key.get_secret_value()
        if not key.strip():
            raise ProviderError("missing_api_key")
        if not self.config.model.strip():
            raise ProviderError("missing_model")
        try:
            request = ChatRequest.model_validate(request.model_dump())
        except ValidationError:
            raise ProviderError("invalid_request") from None
        payload = {"model": self.config.model, **request.model_dump(exclude_none=True), "stream": False}
        try:
            with httpx.Client(timeout=self.config.timeout_seconds, trust_env=False, follow_redirects=False) as client:
                with client.stream("POST", self.config.base_url + "/chat/completions",
                                   headers={"Authorization": "Bearer " + key}, json=payload) as response:
                    status = response.status_code
                    if status != 200:
                        code = ("authentication" if status in {401, 403} else "rate_limit" if status == 429 else
                                "upstream_error" if status >= 500 else "request_rejected")
                        raise ProviderError(code)
                    raw = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        raw.extend(chunk)
                        if len(raw) > 262144:
                            raise ProviderError("response_too_large")
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
                raise ProviderError("invalid_response")
            choice = data["choices"][0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ProviderError("invalid_response")
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise ProviderError("refused")
            if message.get("role") != "assistant":
                raise ProviderError("invalid_response")
            if not isinstance(data.get("model"), str) or not data["model"]:
                raise ProviderError("invalid_response")
            # Redact the whole validated reply, including optional protocol metadata.
            clean = redact({"content": message.get("content"), "model": data["model"],
                "finish_reason": choice["finish_reason"], "tool_calls": message.get("tool_calls", [])}, key)
            reply = ChatReply.model_validate(clean)
            if reply.finish_reason == "tool_calls":
                available = {t.function.name for t in request.tools or []}
                if not reply.tool_calls or any(call.function.name not in available for call in reply.tool_calls):
                    raise ProviderError("invalid_response")
            elif reply.content is None or reply.tool_calls:
                raise ProviderError("invalid_response")
            return reply
        except httpx.TimeoutException:
            raise ProviderError("timeout") from None
        except httpx.HTTPError:
            raise ProviderError("network_error") from None
        except (KeyError, IndexError, TypeError, ValueError, RecursionError):
            raise ProviderError("invalid_response") from None


def provider_for_agent(registry, agent_id: str) -> LLMProvider:
    """Resolve a metadata reference; constructing a provider performs no HTTP."""
    agent = registry.get(agent_id)
    if not agent.enabled:
        raise ProviderError("agent_disabled")
    config = LLMConfig.from_env() if agent.model_profile == "default" else LLMConfig()
    return OpenAICompatibleProvider(config)
