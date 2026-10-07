"""Shared fixtures. TASK-100 skeleton; TASK-106 (service) extends this file."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.engine.fake import FakeEngine
from app.reader.fake import FakeReader

_DEFAULTS = dict(
    FAKE_ENGINE=True,
    CONTROL_TOKEN="test-token",
    OVERLAY_KEY="test-key",
    ANTHROPIC_API_KEY=None,
)


@pytest.fixture
def make_settings(monkeypatch):
    """Factory for hermetic Settings: ignores the developer's .env and every Settings env var."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)

    def _make(**overrides) -> Settings:
        kwargs = {**_DEFAULTS, **overrides}
        return Settings(_env_file=None, **kwargs)

    return _make


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def fake_reader() -> FakeReader:
    return FakeReader()
