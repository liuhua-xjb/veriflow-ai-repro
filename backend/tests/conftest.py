from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]


def pytest_configure(config):
    # Direct pytest and both PowerShell wrappers keep test databases in this repo.
    if config.option.basetemp is None:
        config.option.basetemp = str(ROOT / '.cache' / ('pytest-' + uuid4().hex))
    if not Path(config.option.basetemp).resolve().is_relative_to(ROOT):
        raise pytest.UsageError('pytest basetemp must be inside the project root')


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    assert tmp_path.is_relative_to(ROOT)
    with TestClient(create_app(tmp_path / "agents.sqlite3")) as value:
        yield value


@pytest.fixture
def spec():
    return {"id": "test_analyst", "name": "Test analyst", "role": "analyst",
            "capabilities": ["analysis"], "allowed_tools": ["numeric_summary"]}
