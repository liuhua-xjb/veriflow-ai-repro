import time

import pytest

from app.tools import Numbers, Summary, Tool, ToolRegistry, tools


def test_real_deterministic_tools():
    result = tools.invoke("numeric_summary", {"values": [2.0, 4.0, 6.0]}, allowed_tools=["numeric_summary"], enabled=True)
    assert result.ok and result.output == {"count": 3, "minimum": 2.0, "maximum": 6.0, "mean": 4.0}
    result = tools.invoke("json_fields", {"document": {"x": 1}, "required": ["z", "x"]}, allowed_tools=["json_fields"], enabled=True)
    assert result.ok and result.output == {"valid": False, "missing": ["z"]}


@pytest.mark.parametrize("arguments", [{"values": []}, {"values": [True]}, {"values": ["3"]},
    {"values": [float("nan")]}, {"values": [float("inf")]}, {"values": [1e101]},
    {"values": [1.0] * 1001}, {"values": [1.0], "shell": "x"}])
def test_tool_invalid_input(arguments):
    assert tools.invoke("numeric_summary", arguments, allowed_tools=["numeric_summary"], enabled=True).error == "invalid_input"


def test_permissions_and_schema():
    assert tools.invoke("numeric_summary", {}, allowed_tools=[], enabled=True).error == "permission_denied"
    assert tools.invoke("shell", {}, allowed_tools=["shell"], enabled=True).error == "unknown_tool"
    metadata = tools.discover()
    assert len(metadata) == 2
    assert all(t["parameters"]["additionalProperties"] is False and "properties" in t["output"] for t in metadata)
    for arguments in [{"document": {"x": {}}, "required": ["x"]}, {"document": {}, "required": ["x", "x"]}]:
        assert tools.invoke("json_fields", arguments, allowed_tools=["json_fields"], enabled=True).error == "invalid_input"


def test_timeout_and_execution_failure():
    registry = ToolRegistry()
    def slow(data):
        time.sleep(.05)
    registry.register(Tool("slow", "1", "test-only bounded sleep", Numbers, Summary, slow, .001))
    assert registry.invoke("slow", {"values": [1.0]}, allowed_tools=["slow"], enabled=True).error == "timeout"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Tool("slow", "1", "duplicate", Numbers, Summary, slow))
    def fail(data):
        raise RuntimeError("do not echo internal data")
    registry.register(Tool("fail", "1", "test failure", Numbers, Summary, fail))
    result = registry.invoke("fail", {"values": [1.0]}, allowed_tools=["fail"], enabled=True)
    assert result.error == "execution_error" and "internal" not in result.model_dump_json()
