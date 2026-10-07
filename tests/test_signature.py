"""Signature verification, public key cache and TtlSet. No network: keys are generated locally."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from app.kick.dedupe import TtlSet
from app.kick.signature import PublicKeyCache, http_key_provider, verify_signature

MSG_ID = "01JHZS8C8WPJ5K0P1R1YBRJ8XY"
TS = "2025-01-14T16:08:06Z"
BODY = '{"gift":{"amount":500,"message":"çok güzel"}}'.encode("utf-8")


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    return key, pem


def _sign(key, message_id=MSG_ID, timestamp=TS, body=BODY) -> str:
    signed = f"{message_id}.{timestamp}.".encode("utf-8") + body  # exactly Kick's "%s.%s.%s"
    return base64.b64encode(key.sign(signed, padding.PKCS1v15(), hashes.SHA256())).decode("ascii")


@pytest.fixture(scope="module")
def keys():
    return _keypair()


def test_valid_signature(keys):
    key, pem = keys
    assert verify_signature(MSG_ID, TS, BODY, _sign(key), pem) is True


def test_valid_signature_with_bytes_pem(keys):
    key, pem = keys
    assert verify_signature(MSG_ID, TS, BODY, _sign(key), pem.encode()) is True


@pytest.mark.parametrize(
    "mutate",
    [
        dict(raw_body=BODY + b" "),
        dict(raw_body=BODY.replace(b"500", b"501")),
        dict(timestamp="2025-01-14T16:08:07Z"),
        dict(message_id="01JHZS8C8WPJ5K0P1R1YBRJ8XZ"),
    ],
)
def test_tampered_input_fails(keys, mutate):
    key, pem = keys
    args = dict(message_id=MSG_ID, timestamp=TS, raw_body=BODY, signature_b64=_sign(key), public_key_pem=pem)
    args.update(mutate)
    assert verify_signature(**args) is False


def test_reserialized_body_fails(keys):
    key, pem = keys
    sig = _sign(key)
    reserialized = json.dumps(json.loads(BODY), separators=(", ", ": "), ensure_ascii=False).encode("utf-8")
    assert reserialized != BODY
    assert verify_signature(MSG_ID, TS, reserialized, sig, pem) is False


def test_signature_must_use_dot_separators(keys):
    key, pem = keys
    wrong = base64.b64encode(
        key.sign(f"{MSG_ID}{TS}".encode() + BODY, padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    assert verify_signature(MSG_ID, TS, BODY, wrong, pem) is False


def test_pss_signature_is_rejected(keys):
    key, pem = keys
    signed = f"{MSG_ID}.{TS}.".encode() + BODY
    pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH)
    sig = base64.b64encode(key.sign(signed, pss, hashes.SHA256())).decode()
    assert verify_signature(MSG_ID, TS, BODY, sig, pem) is False


def test_wrong_key_fails(keys):
    key, _ = keys
    _, other_pem = _keypair()
    assert verify_signature(MSG_ID, TS, BODY, _sign(key), other_pem) is False


@pytest.mark.parametrize("sig", ["not base64 !!!", "", "====", "abc", "AAAA"])
def test_bad_signature_encoding_fails(keys, sig):
    _, pem = keys
    assert verify_signature(MSG_ID, TS, BODY, sig, pem) is False


@pytest.mark.parametrize("pem", ["", "garbage", "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"])
def test_bad_pem_fails_without_raising(keys, pem):
    key, _ = keys
    assert verify_signature(MSG_ID, TS, BODY, _sign(key), pem) is False


def test_non_rsa_key_fails(keys):
    key, _ = keys
    ec_pem = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    assert verify_signature(MSG_ID, TS, BODY, _sign(key), ec_pem) is False


def test_garbage_types_never_raise():
    assert verify_signature(None, None, None, None, None) is False  # type: ignore[arg-type]


# ---- PublicKeyCache ---------------------------------------------------------------------------


class Provider:
    def __init__(self, *pems, fail=False):
        self.pems = list(pems)
        self.calls = 0
        self.fail = fail

    async def __call__(self) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return self.pems[min(self.calls, len(self.pems)) - 1]


async def test_cache_fetches_once_and_reuses(keys):
    key, pem = keys
    provider = Provider(pem)
    cache = PublicKeyCache(provider)
    assert provider.calls == 0  # lazy
    for _ in range(3):
        assert await cache.verify(MSG_ID, TS, BODY, _sign(key)) is True
    assert provider.calls == 1


async def test_invalid_signature_with_fresh_key_does_not_refetch(keys):
    _, pem = keys
    provider = Provider(pem)
    cache = PublicKeyCache(provider)
    assert await cache.verify(MSG_ID, TS, BODY, "AAAA") is False
    assert provider.calls == 1  # the key was fetched for this request already


async def test_cache_refetches_exactly_once_after_failure_then_still_false(keys):
    key, pem = keys
    provider = Provider(pem)
    cache = PublicKeyCache(provider)
    assert await cache.verify(MSG_ID, TS, BODY, _sign(key)) is True
    assert provider.calls == 1
    assert await cache.verify(MSG_ID, TS, BODY + b"x", _sign(key)) is False
    assert provider.calls == 2  # exactly one re-fetch for the failure


async def test_key_rotation_recovers_with_one_refetch(keys):
    old_key, old_pem = keys
    new_key, new_pem = _keypair()
    provider = Provider(old_pem, new_pem)
    cache = PublicKeyCache(provider)
    assert await cache.verify(MSG_ID, TS, BODY, _sign(old_key)) is True
    assert provider.calls == 1
    assert await cache.verify(MSG_ID, TS, BODY, _sign(new_key)) is True  # rotated
    assert provider.calls == 2
    assert await cache.verify(MSG_ID, TS, BODY, _sign(new_key)) is True  # cached again
    assert provider.calls == 2


async def test_provider_error_returns_false_and_is_logged(keys, caplog):
    key, _ = keys
    provider = Provider(fail=True)
    cache = PublicKeyCache(provider)
    with caplog.at_level("ERROR", logger="app.kick.signature"):
        assert await cache.verify(MSG_ID, TS, BODY, _sign(key)) is False
    assert provider.calls == 1
    assert any("fetch failed" in r.message for r in caplog.records)


async def test_provider_error_on_refetch_keeps_old_key(keys):
    key, pem = keys
    provider = Provider(pem)
    cache = PublicKeyCache(provider)
    assert await cache.verify(MSG_ID, TS, BODY, _sign(key)) is True
    provider.fail = True
    assert await cache.verify(MSG_ID, TS, BODY + b"x", _sign(key)) is False
    provider.fail = False
    assert await cache.verify(MSG_ID, TS, BODY, _sign(key)) is True  # old key still cached
    assert provider.calls == 2


async def test_provider_recovers_after_initial_error(keys):
    key, pem = keys
    provider = Provider(pem, fail=True)
    cache = PublicKeyCache(provider)
    assert await cache.verify(MSG_ID, TS, BODY, _sign(key)) is False
    provider.fail = False
    assert await cache.verify(MSG_ID, TS, BODY, _sign(key)) is True


async def test_empty_key_from_provider_is_rejected(keys):
    key, _ = keys
    cache = PublicKeyCache(Provider(""))
    assert await cache.verify(MSG_ID, TS, BODY, _sign(key)) is False


async def test_concurrent_failures_share_one_refetch(keys):
    import asyncio

    old_key, old_pem = keys
    new_key, new_pem = _keypair()
    provider = Provider(old_pem, new_pem)
    cache = PublicKeyCache(provider)
    assert await cache.verify(MSG_ID, TS, BODY, _sign(old_key)) is True
    results = await asyncio.gather(*(cache.verify(MSG_ID, TS, BODY, _sign(new_key)) for _ in range(5)))
    assert results == [True] * 5
    assert provider.calls == 2


# ---- http_key_provider (MockTransport) --------------------------------------------------------


async def test_http_provider_parses_openapi_shape(keys):
    _, pem = keys
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": {"public_key": pem}, "message": "OK"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        got = await http_key_provider("https://api.kick.com/public/v1/public-key", client=client)()
    assert got == pem
    assert seen == ["https://api.kick.com/public/v1/public-key"]


async def test_http_provider_accepts_bare_pem(keys):
    _, pem = keys
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=pem))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await http_key_provider("https://x/key", client=client)() == pem.strip()


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(200, json={"data": {}}), httpx.Response(200, json=[1])],
)
async def test_http_provider_errors_raise_and_cache_returns_false(keys, response):
    key, _ = keys
    transport = httpx.MockTransport(lambda r: response)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = http_key_provider("https://x/key", client=client)
        with pytest.raises(Exception):
            await provider()
        assert await PublicKeyCache(provider).verify(MSG_ID, TS, BODY, _sign(key)) is False


# ---- TtlSet -----------------------------------------------------------------------------------


def test_ttl_set_dedupes_and_expires():
    now = [100.0]
    s = TtlSet(600, clock=lambda: now[0])
    assert s.seen_or_add("a") is False
    assert s.seen_or_add("a") is True
    assert s.seen_or_add("b") is False
    now[0] += 599
    assert s.seen_or_add("a") is True
    now[0] += 2  # a and b expired (TTL is not extended by repeat sightings)
    assert s.seen_or_add("a") is False
    assert len(s) == 1


def test_ttl_set_max_size_evicts_oldest():
    s = TtlSet(600, clock=lambda: 0.0, max_size=3)
    for k in "abc":
        assert s.seen_or_add(k) is False
    assert s.seen_or_add("d") is False
    assert len(s) == 3
    assert s.seen_or_add("a") is False  # evicted, so new again
    assert s.seen_or_add("d") is True
