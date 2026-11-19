import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_health_contract():
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok", "service": "veriflow-api", "version": "0.1.0"
    }


@pytest.mark.parametrize("origin", ["http://127.0.0.1:15173", "http://localhost:15173"])
def test_local_cors(origin):
    with TestClient(app) as client:
        response = client.get("/api/health", headers={"Origin": origin})
    assert response.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in response.headers


def test_external_cors_rejected():
    with TestClient(app) as client:
        response = client.options("/api/health", headers={
            "Origin": "https://example.com", "Access-Control-Request-Method": "GET"
        })
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
