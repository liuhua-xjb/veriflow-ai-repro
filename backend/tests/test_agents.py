import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from app.agents import AgentCreate, AgentSpec
from app.registry import AgentRegistry, RegistryError
from app.tools import invoke_for_agent


@pytest.mark.parametrize("change", [
    {"name": " "}, {"name": 4}, {"role": "root"}, {"capabilities": []},
    {"capabilities": ["analysis", "analysis"]}, {"capabilities": ["Bad Name"]},
    {"allowed_tools": ["shell"]}, {"allowed_tools": ["json_fields", "json_fields"]},
    {"concurrency_limit": -1}, {"concurrency_limit": 0}, {"concurrency_limit": "2"},
    {"concurrency_limit": True}, {"enabled": "false"}, {"version": 2},
    {"id": "../escape"}, {"memory_policy": "unlimited"}, {"model_profile": {"api_key": "no"}},
    {"api_key": "synthetic-secret-never-reflect"}, {"description": "x" * 2001},
    {"version": True}, {"version": "1"},
])
def test_invalid_schema(client, spec, change):
    response = client.post("/api/agents", json={**spec, **change})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "synthetic-secret-never-reflect" not in response.text


def test_versions_and_conflicts(client, spec):
    one = client.post("/api/agents", json=spec)
    assert one.status_code == 201
    assert one.json()["version"] == 1
    assert one.json()["created_at"].endswith("Z")
    assert client.post("/api/agents", json=spec).status_code == 409
    path = "/api/agents/" + spec["id"]
    change = {k: v for k, v in spec.items() if k != "id"}
    change.update(name="Revised", base_version=1)
    two = client.post(path + "/versions", json=change)
    assert two.status_code == 201 and two.json()["version"] == 2
    assert client.post(path + "/versions", json=change).status_code == 409
    assert client.get(path, params={"version": 1}).json()["name"] == spec["name"]
    assert client.get(path).json()["name"] == "Revised"
    assert [a["version"] for a in client.get(path + "/versions").json()] == [1, 2]
    assert client.patch(path + "/enabled", json={"enabled": False, "base_version": 2}).json()["version"] == 3
    assert client.patch(path + "/enabled", json={"enabled": True, "base_version": 2}).status_code == 409
    assert client.get(path, params={"version": 1}).json()["enabled"] is True
    assert client.get(path, params={"version": 0}).status_code == 422


@pytest.mark.parametrize("path,method,body", [
    ("/api/agents/missing", "get", None),
    ("/api/agents/missing/versions", "get", None),
    ("/api/agents/missing/enabled", "patch", {"enabled": False, "base_version": 1}),
])
def test_not_found(client, path, method, body):
    response = getattr(client, method)(path, **({"json": body} if body else {}))
    assert response.status_code == 404


def test_discovery_and_templates(client, spec):
    assert len(client.get("/api/agent-templates").json()) == 3
    for id_, enabled, allowed in [("z", True, ["numeric_summary"]), ("a", True, []), ("b", False, ["numeric_summary"])]:
        assert client.post("/api/agents", json={**spec, "id": id_, "enabled": enabled, "allowed_tools": allowed}).status_code == 201
    assert [a["id"] for a in client.get("/api/agents").json()] == ["a", "b", "z"]
    assert [a["id"] for a in client.get("/api/agents?enabled=false").json()] == ["b"]
    matches = client.get("/api/agents/discover?capability=analysis&tool=numeric_summary").json()
    assert [m["agent"]["id"] for m in matches] == ["z"]
    assert matches[0]["reason"] == {"capability": "analysis", "enabled": True, "tool": "numeric_summary"}
    assert client.get("/api/agents/discover?capability=missing").json() == []
    assert client.get("/api/agents?tool=shell").json() == []


def test_persistence_and_authorization(tmp_path, spec):
    path = tmp_path / "registry.sqlite3"
    registry = AgentRegistry(path)
    registry.create(AgentCreate(**spec))
    reopened = AgentRegistry(path)
    assert reopened.get(spec["id"]).name == spec["name"]
    result = invoke_for_agent(reopened, spec["id"], "numeric_summary", {"values": [1.0, 2.0, 3.0]})
    assert result.output == {"count": 3, "minimum": 1.0, "maximum": 3.0, "mean": 2.0}
    assert invoke_for_agent(reopened, spec["id"], "json_fields", {"document": {}, "required": ["x"]}).error == "permission_denied"
    reopened.revise(spec["id"], 1, enabled=False)
    assert invoke_for_agent(reopened, spec["id"], "numeric_summary", {"values": [1.0]}).error == "permission_denied"
    assert "api_key" not in path.read_bytes().decode("latin1").lower()


def test_atomic_version_conflict(tmp_path, spec):
    registry = AgentRegistry(tmp_path / "race.sqlite3")
    registry.create(AgentCreate(**spec))
    def revise(_):
        try:
            return registry.revise(spec["id"], 1, enabled=False).version
        except RegistryError as e:
            return e.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(revise, range(2))) == [2, 409]
    assert len(registry.versions(spec["id"])) == 2


def test_unknown_database_version(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="unsupported database schema"):
        AgentRegistry(path)


def test_cors_and_safe_errors(client):
    for method in ["POST", "PATCH"]:
        response = client.options("/api/agents", headers={"Origin": "http://127.0.0.1:15173",
            "Access-Control-Request-Method": method, "Access-Control-Request-Headers": "content-type"})
        assert response.status_code == 200
    assert client.options("/api/agents", headers={"Origin": "https://example.com",
        "Access-Control-Request-Method": "POST"}).status_code == 400
    assert client.options("/api/agents", headers={"Origin": "http://127.0.0.1:15173",
        "Access-Control-Request-Method": "DELETE"}).status_code == 400
    response = client.post("/api/agents", json={"synthetic-secret-in-key": "synthetic-secret-in-value"})
    assert "synthetic-secret" not in response.text
    assert "/api/agents" in client.get("/openapi.json").json()["paths"]
    assert client.post("/api/agents", content="{broken", headers={"Content-Type": "application/json"}).status_code == 400
