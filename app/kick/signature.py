"""Kick webhook signature verification (RSA PKCS#1 v1.5 / SHA-256) and the public-key cache.

Kick signs ``f"{Kick-Event-Message-Id}.{Kick-Event-Message-Timestamp}.{raw body}"`` with its private
key (docs/kick/webhook-security.md). The public key is fetched from the hosted endpoint, never
hardcoded, and may rotate at any time, so a failed verification triggers exactly one re-fetch.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Awaitable, Callable

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger(__name__)

KeyProvider = Callable[[], Awaitable[str]]

DEFAULT_KEY_URL = "https://api.kick.com/public/v1/public-key"


def verify_signature(
    message_id: str,
    timestamp: str,
    raw_body: bytes,
    signature_b64: str,
    public_key_pem: str | bytes,
) -> bool:
    """True only when the signature over b"{message_id}.{timestamp}." + raw_body verifies. Never raises."""
    try:
        signature = base64.b64decode(signature_b64.strip(), validate=True)
        pem = public_key_pem.encode("utf-8") if isinstance(public_key_pem, str) else bytes(public_key_pem)
        key = serialization.load_pem_public_key(pem)
        if not isinstance(key, rsa.RSAPublicKey):
            return False
        signed = f"{message_id}.{timestamp}.".encode("utf-8") + bytes(raw_body)
        key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:  # bad base64, bad PEM, wrong key type, InvalidSignature, non-str input ...
        return False


class PublicKeyCache:
    """Caches the Kick public key; on a failed verification re-fetches once and retries."""

    def __init__(self, key_provider: KeyProvider) -> None:
        self._provider = key_provider
        self._pem: str | None = None
        self._lock = asyncio.Lock()

    async def _refresh(self, stale: str | None) -> str | None:
        """Fetch a new key. If another task already replaced `stale` while we waited, reuse its result."""
        async with self._lock:
            if self._pem is not None and self._pem != stale:
                return self._pem
            try:
                pem = await self._provider()
            except Exception as exc:
                log.error("kick public key fetch failed: %s", type(exc).__name__)
                return None
            if not isinstance(pem, str) or not pem.strip():
                log.error("kick public key provider returned an empty key")
                return None
            self._pem = pem
            return pem

    async def verify(
        self,
        message_id: str,
        timestamp: str,
        raw_body: bytes,
        signature_b64: str,
    ) -> bool:
        pem = self._pem
        fresh = False
        if pem is None:
            pem = await self._refresh(None)
            if pem is None:
                return False
            fresh = True
        if verify_signature(message_id, timestamp, raw_body, signature_b64, pem):
            return True
        if fresh:  # the key was fetched for this very request; a second fetch cannot help
            return False
        new_pem = await self._refresh(pem)  # possible rotation: re-fetch once and retry
        if new_pem is None or new_pem == pem:
            return False
        return verify_signature(message_id, timestamp, raw_body, signature_b64, new_pem)


def http_key_provider(
    url: str = DEFAULT_KEY_URL,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 5.0,
) -> KeyProvider:
    """Provider that GETs the Kick public-key endpoint.

    openapi.yaml: 200 -> {"data": {"public_key": "<PEM>"}, "message": "..."}. The Go example in
    webhook-security.md treats the body as a bare PEM, so both shapes are accepted.
    """

    async def provider() -> str:
        if client is not None:
            resp = await client.get(url, timeout=timeout_s)
        else:
            async with httpx.AsyncClient(timeout=timeout_s) as c:
                resp = await c.get(url)
        resp.raise_for_status()
        text = resp.text.strip()
        if text.startswith("-----BEGIN"):
            return text
        body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        pem = data.get("public_key") if isinstance(data, dict) else None
        if not isinstance(pem, str) or not pem.strip():
            raise ValueError("public key response has no data.public_key")
        return pem

    return provider
