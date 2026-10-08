"""Shared fixtures. TASK-100 skeleton (settings, fake engine/reader) extended by TASK-106 (service)."""
from __future__ import annotations

import base64
import os
import threading
import uuid

# `import app.main` builds `app = create_app(Settings())` at import time; without FAKE_ENGINE the
# Settings validator demands CONTROL_TOKEN/OVERLAY_KEY. Tests never rely on that module-level app
# (they call create_app with hermetic settings), so a dev default here is enough for collection.
os.environ.setdefault("FAKE_ENGINE", "1")

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import Settings
from app.engine.fake import FakeEngine
from app.reader.fake import FakeReader

_DEFAULTS = dict(
    FAKE_ENGINE=True,
    CONTROL_TOKEN="test-token",
    OVERLAY_KEY="test-key",
    ANTHROPIC_API_KEY=None,
    SETTINGS_PATH="",  # hermetic: do not read app/settings.yaml
)


@pytest.fixture
def make_settings(monkeypatch, tmp_path):
    """Factory for hermetic Settings: ignores the developer's .env and every Settings env var."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)

    def _make(**overrides) -> Settings:
        # panel overrides go to a per-test file, never to ./.cache
        kwargs = {**_DEFAULTS, "PANEL_STATE_PATH": tmp_path / "panel-settings.json", **overrides}
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


# ---- TASK-106: engines for readiness tests -----------------------------------------------------


class BlockingFakeEngine(FakeEngine):
    """warmup() blocks until `release()` is called (or the safety timeout elapses), so tests can
    observe the 'warming' state. Always released on fixture teardown so the executor thread exits."""

    name = "blocking-fake"

    def __init__(self, timeout_s: float = 15.0) -> None:
        self.gate = threading.Event()
        self.started = threading.Event()
        self.warmups = 0
        self._timeout_s = timeout_s

    def warmup(self) -> None:
        self.started.set()
        self.gate.wait(self._timeout_s)
        self.warmups += 1

    def release(self) -> None:
        self.gate.set()


class FailingFakeEngine(FakeEngine):
    name = "failing-fake"

    class WeightsCorrupt(RuntimeError):
        pass

    def warmup(self) -> None:
        raise self.WeightsCorrupt("bad weights")


@pytest.fixture
def blocking_engine():
    eng = BlockingFakeEngine()
    yield eng
    eng.release()


@pytest.fixture
def failing_engine() -> FailingFakeEngine:
    return FailingFakeEngine()


# ---- TASK-106: Kick webhook signing ------------------------------------------------------------


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    return key, pem


@pytest.fixture(scope="session")
def rsa_keypair():
    """(private_key, public_pem) generated once per session; nothing is ever fetched from Kick."""
    return _keypair()


@pytest.fixture
def make_keypair():
    """Factory for extra (private_key, public_pem) pairs, e.g. to simulate a Kick key rotation."""
    return _keypair


def _sign_webhook(
    private_key,
    body: bytes,
    *,
    event_type: str,
    message_id: str | None = None,
    timestamp: str = "2025-01-14T16:08:06Z",
    subscription_id: str = "01JHZS8C8WPJ5K0P1R1YBRJ8XY",
    version: str = "1",
) -> dict[str, str]:
    """The six Kick-Event-* headers for `body`, signed PKCS#1 v1.5 / SHA-256 over
    f"{message_id}.{timestamp}.{raw_body}" exactly as docs/kick/webhook-security.md describes."""
    message_id = message_id or uuid.uuid4().hex
    signed = f"{message_id}.{timestamp}.".encode("utf-8") + body
    sig = base64.b64encode(private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())).decode("ascii")
    return {
        "Kick-Event-Message-Id": message_id,
        "Kick-Event-Subscription-Id": subscription_id,
        "Kick-Event-Signature": sig,
        "Kick-Event-Message-Timestamp": timestamp,
        "Kick-Event-Type": event_type,
        "Kick-Event-Version": version,
        "Content-Type": "application/json",
    }


@pytest.fixture
def sign_webhook():
    """sign_webhook(private_key, body, event_type=..., **kw) -> the six signed Kick headers."""
    return _sign_webhook


@pytest.fixture
def sign(rsa_keypair):
    """sign(body, event_type=..., **kw) -> headers, bound to the session key pair."""
    key, _ = rsa_keypair

    def _sign(body: bytes, **kw) -> dict[str, str]:
        return _sign_webhook(key, body, **kw)

    return _sign


class CountingKeyProvider:
    """Async key provider that counts calls and can serve a sequence of PEMs (rotation tests)."""

    def __init__(self, *pems: str) -> None:
        self.pems = list(pems)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return self.pems[min(self.calls, len(self.pems)) - 1]


@pytest.fixture
def make_key_provider():
    """make_key_provider(pem1, pem2, ...) -> CountingKeyProvider serving the PEMs in order."""
    return CountingKeyProvider


@pytest.fixture
def key_provider(rsa_keypair) -> CountingKeyProvider:
    _, pem = rsa_keypair
    return CountingKeyProvider(pem)
