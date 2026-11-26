import pytest

from vingo_agent.config import get_settings


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("AGENT_SERVICE_KEY", "test-service-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
