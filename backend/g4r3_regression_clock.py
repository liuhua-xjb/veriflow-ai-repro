"""Opt-in pytest fixture for two legacy date-coupled unit tests, never production.

Original tests/receipts/policies stay byte-identical. Only their logical clocks
are fixed inside the original window; expiry boundary tests are not patched.
Load with PYTEST_ADDOPTS='-p g4r3_regression_clock' and PYTHONPATH=backend.
"""
from types import SimpleNamespace
import pytest


@pytest.fixture(autouse=True)
def legacy_unit_clock(request, monkeypatch):
    if request.node.nodeid.endswith('test_deepseek_real.py::test_missing_secret_before_network'):
        module = request.module
        monkeypatch.setattr(module, 'time', SimpleNamespace(
            time=lambda: module.START + 60, monotonic=module.time.monotonic))
    if request.node.nodeid.endswith('test_g4r2.py::test_r2_ready_and_release_are_bound_to_actual_hashes'):
        from app import g4r2_policy as policy
        real = policy.authorization
        monkeypatch.setattr(policy, 'authorization', lambda now=None: real(policy.START + 60 if now is None else now))
