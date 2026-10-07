"""POST /webhook/kick: raw-byte signature check, exact headers, dedupe, dispatch, fast 200.

Payloads are the TASK-104 fixtures (tests/data/kick/*.json, the doc payloads). The key pair is
generated per session; the key provider is injected and counted, so nothing touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import KICK_HEADERS, create_app
from app.metrics import (
    get_value,
    tts_events_received_total,
    tts_items_enqueued_total,
    tts_webhook_rejected_total,
)
DATA = Path(__file__).parent / "data" / "kick"
KICKS, REWARD, CHAT = "kicks.gifted", "channel.reward.redemption.updated", "chat.message.sent"
AUTH = {"Authorization": "Bearer test-token"}


def _body(name: str, **mutate) -> bytes:
    payload = json.loads((DATA / name).read_text(encoding="utf-8"))
    for key, value in mutate.items():
        payload[key] = value
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def kicks_body() -> bytes:
    return _body("kicks_gifted.json")


@pytest.fixture
def app_and_provider(settings, blocking_engine, fake_reader, key_provider):
    """App whose warm-up never completes during the test: proves the webhook acks while warming."""
    app = create_app(settings, engine=blocking_engine, reader=fake_reader, key_provider=key_provider)
    return app, key_provider


def test_kick_header_names_are_exact():
    assert KICK_HEADERS == (
        "Kick-Event-Message-Id",
        "Kick-Event-Subscription-Id",
        "Kick-Event-Signature",
        "Kick-Event-Message-Timestamp",
        "Kick-Event-Type",
        "Kick-Event-Version",
    )


def test_valid_signature_enqueues_while_warming_without_clients(app_and_provider, sign, kicks_body):
    app, provider = app_and_provider
    before_enq = get_value(tts_items_enqueued_total, kind="kicks")
    before_recv = get_value(tts_events_received_total, type=KICKS)
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 503  # still warming
        assert c.get("/status", headers=AUTH).json()["clients"] == 0
        headers = sign(kicks_body, event_type=KICKS)
        r = c.post("/webhook/kick", content=kicks_body, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "queued"
        assert get_value(tts_items_enqueued_total, kind="kicks") == before_enq + 1
        assert get_value(tts_events_received_total, type=KICKS) == before_recv + 1
        assert provider.calls == 1
        items = app.state.queue.snapshot()
        assert len(items) == 1
        item = items[0]
        assert item.kind == "kicks" and item.user == "gift_sender"
        assert item.raw_text.startswith("gift_sender 500 kick gönderdi")
        assert item.meta["kick_message_id"] == headers["Kick-Event-Message-Id"]
        assert c.get("/readyz").status_code == 503  # the request path never waited for warm-up


def test_tampered_body_is_401(app_and_provider, sign, kicks_body):
    app, provider = app_and_provider
    before = get_value(tts_webhook_rejected_total, reason="bad_signature")
    with TestClient(app) as c:
        headers = sign(kicks_body, event_type=KICKS)
        tampered = kicks_body.replace(b"500", b"501")
        r = c.post("/webhook/kick", content=tampered, headers=headers)
        assert r.status_code == 401
        assert len(app.state.queue) == 0
        assert get_value(tts_webhook_rejected_total, reason="bad_signature") == before + 1
        # signature must be over the raw bytes: re-serialised JSON with different whitespace fails too
        reserialized = json.dumps(json.loads(kicks_body), indent=2).encode("utf-8")
        assert reserialized != kicks_body
        assert c.post("/webhook/kick", content=reserialized, headers=headers).status_code == 401
        # a wrong key (different signer) fails as well
        bad_sig = dict(headers, **{"Kick-Event-Signature": "AAAA"})
        assert c.post("/webhook/kick", content=kicks_body, headers=bad_sig).status_code == 401


@pytest.mark.parametrize("missing", list(KICK_HEADERS))
def test_missing_header_is_400(app_and_provider, sign, kicks_body, missing):
    app, provider = app_and_provider
    before = get_value(tts_webhook_rejected_total, reason="missing_headers")
    with TestClient(app) as c:
        headers = sign(kicks_body, event_type=KICKS)
        del headers[missing]
        r = c.post("/webhook/kick", content=kicks_body, headers=headers)
        assert r.status_code == 400
        assert get_value(tts_webhook_rejected_total, reason="missing_headers") == before + 1
        assert provider.calls == 0  # rejected before any key fetch
        assert len(app.state.queue) == 0


def test_no_headers_at_all_is_400(app_and_provider, kicks_body):
    app, _ = app_and_provider
    with TestClient(app) as c:
        assert c.post("/webhook/kick", content=kicks_body).status_code == 400


def test_duplicate_message_id_is_acked_once(app_and_provider, sign, kicks_body):
    app, _ = app_and_provider
    before = get_value(tts_items_enqueued_total, kind="kicks")
    with TestClient(app) as c:
        headers = sign(kicks_body, event_type=KICKS, message_id="01JHZS8C8WPJ5K0P1R1YBRJ8XY")
        r1 = c.post("/webhook/kick", content=kicks_body, headers=headers)
        r2 = c.post("/webhook/kick", content=kicks_body, headers=headers)
        assert r1.json()["status"] == "queued"
        assert r2.status_code == 200 and r2.json() == {"status": "duplicate"}
        assert len(app.state.queue) == 1
        assert get_value(tts_items_enqueued_total, kind="kicks") == before + 1


def test_key_provider_refetched_once_after_failure_and_rotation_recovers(
    settings, blocking_engine, fake_reader, rsa_keypair, make_keypair, make_key_provider, sign_webhook, kicks_body
):
    old_key, old_pem = rsa_keypair
    new_key, new_pem = make_keypair()
    provider = make_key_provider(old_pem, new_pem)
    app = create_app(settings, engine=blocking_engine, reader=fake_reader, key_provider=provider)
    with TestClient(app) as c:
        ok = sign_webhook(old_key, kicks_body, event_type=KICKS)
        assert c.post("/webhook/kick", content=kicks_body, headers=ok).status_code == 200
        assert provider.calls == 1
        # a bad signature triggers exactly one re-fetch (which here hands out the rotated key), then 401
        bad = dict(ok, **{"Kick-Event-Signature": "AAAA", "Kick-Event-Message-Id": "other-id"})
        assert c.post("/webhook/kick", content=kicks_body, headers=bad).status_code == 401
        assert provider.calls == 2
        # the rotated key is now cached: a message signed with it verifies without another fetch
        rotated = sign_webhook(new_key, kicks_body, event_type=KICKS)
        assert c.post("/webhook/kick", content=kicks_body, headers=rotated).status_code == 200
        assert provider.calls == 2


def test_dispatch_uses_header_not_body(app_and_provider, sign):
    """The same chat payload is ignored under an unsupported type and mapped under chat.message.sent."""
    app, _ = app_and_provider
    body = _body("chat_message_sent.json", content="!tts selam millet")
    with TestClient(app) as c:
        r = c.post("/webhook/kick", content=body, headers=sign(body, event_type="channel.followed"))
        assert r.status_code == 200 and r.json() == {"status": "ignored"}
        assert len(app.state.queue) == 0
        r = c.post("/webhook/kick", content=body, headers=sign(body, event_type=CHAT))
        assert r.json()["status"] == "queued"
        item = app.state.queue.snapshot()[0]
        assert item.kind == "command" and item.raw_text == "selam millet"


def test_reward_and_rejected_reward(app_and_provider, sign):
    app, _ = app_and_provider
    tts_reward = {"id": "01KBHE7RZNHB0SKDV1H86CD4F3", "title": "TTS", "cost": 1000, "description": "tts"}
    rejected = _body("channel_reward_redemption_updated.json", reward=tts_reward)  # doc status: rejected
    accepted = _body(
        "channel_reward_redemption_updated.json", reward=tts_reward, status="accepted", user_input="merhaba yayın"
    )
    with TestClient(app) as c:
        r = c.post("/webhook/kick", content=rejected, headers=sign(rejected, event_type=REWARD))
        assert r.json() == {"status": "ignored"}
        r = c.post("/webhook/kick", content=accepted, headers=sign(accepted, event_type=REWARD))
        assert r.json()["status"] == "queued"
        item = app.state.queue.snapshot()[0]
        assert item.kind == "reward" and item.raw_text == "merhaba yayın"


def test_invalid_json_with_valid_signature_is_acked_and_ignored(app_and_provider, sign):
    app, _ = app_and_provider
    body = b"this is not json"
    with TestClient(app) as c:
        r = c.post("/webhook/kick", content=body, headers=sign(body, event_type=KICKS))
        assert r.status_code == 200 and r.json() == {"status": "ignored"}
        assert len(app.state.queue) == 0


def test_webhook_during_warmup_and_then_spoken_when_ready(settings, blocking_engine, fake_reader, key_provider, sign, kicks_body):
    """End to end: queued while warming, then delivered to an overlay once warm-up completes."""
    app = create_app(settings, engine=blocking_engine, reader=fake_reader, key_provider=key_provider)
    with TestClient(app) as c:
        assert c.post("/webhook/kick", content=kicks_body, headers=sign(kicks_body, event_type=KICKS)).status_code == 200
        with c.websocket_connect("/ws?key=test-key") as ws:
            blocking_engine.release()
            msg = ws.receive_json()
            assert msg["type"] == "item" and msg["user"] == "gift_sender"
            assert msg["caption"].startswith("gift_sender 500 kick gönderdi")
            ws.send_json({"type": "played", "id": msg["id"]})
