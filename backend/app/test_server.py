"""Explicit local TEST_ONLY adapter. Never the default server entry point."""
from .main import create_app
app=create_app(test_only=True)
