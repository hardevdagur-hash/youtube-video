"""Smoke tests talk to a deployed stack over HTTP and need only pytest + httpx.

The root tests/conftest.py has an autouse fixture that imports application code
(config, Groq, dotenv) for unit tests. Overriding it here by name keeps the smoke
suite runnable from any machine, CI included, without the backend dependencies.
"""

import pytest


@pytest.fixture(autouse=True)
def _fresh_translation_service():
    """No-op: smoke tests never import the application."""
    yield
