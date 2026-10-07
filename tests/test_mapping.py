"""EventMapper tests against the Kick doc payloads checked in under tests/data/kick/.

Fixtures are docs/kick/event-types.md with only the `//` comments (and, for kicks.gifted, the HTML
<pre><code> wrapper) removed, plus trailing commas dropped so they parse as JSON. Keys, nesting and example
values are unchanged. Each test mutates only the scenario fields.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.kick.events import EventMapper
from app.metrics import get_value, tts_items_dropped_total

DATA = Path(__file__).parent / "data" / "kick"

# fixture file -> source block in docs/kick/event-types.md
SOURCES = {
    "chat_message_sent.json": "docs/kick/event-types.md lines 33-101 (Chat Message)",
    "channel_reward_redemption_updated.json": "docs/kick/event-types.md lines 254-279 (Channel Reward Redemption Updated)",
    "kicks_gifted.json": "docs/kick/event-types.md lines 411-435 (Kicks Gifted)",
}

CHAT, REWARD, KICKS = "chat.message.sent", "channel.reward.redemption.updated", "kicks.gifted"


def _load(name: str) -> dict:
    assert name in SOURCES
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@pytest.fixture
def kicks():
    return _load("kicks_gifted.json")


@pytest.fixture
def reward():
    return _load("channel_reward_redemption_updated.json")


@pytest.fixture
def chat():
    return _load("chat_message_sent.json")


@pytest.fixture
def mapper(settings):
    return EventMapper(settings)


def _dropped(reason: str) -> float:
    return get_value(tts_items_dropped_total, reason=reason)


def _chat(chat, content="!tts selam", badges=..., identity=..., sender_id=None):
    p = copy.deepcopy(chat)
    p["content"] = content
    if identity is not ...:
        p["sender"]["identity"] = identity
    if badges is not ...:
        p["sender"]["identity"] = {"username_color": "#fff", "badges": badges}
    if sender_id is not None:
        p["sender"]["user_id"] = sender_id
    return p


def test_fixtures_match_doc_shapes(kicks, reward, chat):
    assert kicks["sender"]["username"] == "gift_sender" and kicks["gift"]["amount"] == 500
    assert kicks["gift"]["message"] == "w"
    assert reward["id"] == "01KBHE78QE4HZY1617DK5FC7YD" and reward["status"] == "rejected"
    assert [b["type"] for b in chat["sender"]["identity"]["badges"]] == ["moderator", "sub_gifter", "subscriber"]
    assert chat["broadcaster"]["identity"] is None


# ---- kicks.gifted -----------------------------------------------------------------------------


def test_kicks_gifted(mapper, kicks):
    item = mapper.map(KICKS, kicks, message_id="01MSG")
    assert item is not None
    assert item.kind == "kicks" and item.priority == 0
    assert item.user == "gift_sender"
    assert item.raw_text.startswith("gift_sender 500 kick gönderdi")
    assert item.raw_text == "gift_sender 500 kick gönderdi: w"
    assert item.meta["amount"] == 500 and item.meta["kick_message_id"] == "01MSG"


def test_kicks_empty_or_emoji_message_drops_suffix(mapper, kicks):
    for msg in ("", None, "   ", "😂😂"):
        kicks["gift"]["message"] = msg
        assert mapper.map(KICKS, kicks).raw_text == "gift_sender 500 kick gönderdi"


def test_kicks_below_min(make_settings, kicks):
    m = EventMapper(make_settings(MIN_KICKS=501))
    before = _dropped("below_min_kicks")
    assert m.map(KICKS, kicks) is None
    assert _dropped("below_min_kicks") == before + 1
    kicks["gift"]["amount"] = 501
    assert m.map(KICKS, kicks) is not None


@pytest.mark.parametrize("sender", [{"is_anonymous": True, "user_id": None, "username": None}, None, {"username": None}])
def test_kicks_anonymous(mapper, kicks, sender):
    kicks["sender"] = sender
    item = mapper.map(KICKS, kicks)
    assert item is not None and item.user == "anonim"
    assert item.raw_text.startswith("anonim 500 kick gönderdi")


def test_kicks_anonymous_flag_hides_username(mapper, kicks):
    kicks["sender"]["is_anonymous"] = True
    assert mapper.map(KICKS, kicks).user == "anonim"


def test_kicks_missing_gift_is_invalid_payload(mapper, kicks):
    del kicks["gift"]
    before = _dropped("invalid_payload")
    assert mapper.map(KICKS, kicks) is None
    assert _dropped("invalid_payload") == before + 1


# ---- channel.reward.redemption.updated --------------------------------------------------------


def _redeem(reward, title="TTS", status="accepted", user_input=None, rid=None):
    p = copy.deepcopy(reward)
    p["reward"]["title"] = title
    p["status"] = status
    if user_input is not None:
        p["user_input"] = user_input
    if rid is not None:
        p["id"] = rid
    return p


def test_reward_accepted(mapper, reward):
    item = mapper.map(REWARD, _redeem(reward))
    assert item is not None
    assert item.kind == "reward" and item.priority == 1
    assert item.raw_text == reward["user_input"] == "unban me"
    assert item.user == "naughty-user"
    assert item.meta["redemption_id"] == reward["id"] and item.meta["reward_title"] == "TTS"


@pytest.mark.parametrize("title", ["tts", "Tts", " TTS "])
def test_reward_title_case_insensitive(mapper, reward, title):
    assert mapper.map(REWARD, _redeem(reward, title=title)) is not None


def test_reward_title_turkish_case(make_settings, reward):
    m = EventMapper(make_settings(REWARD_TITLE="Işık"))
    assert m.map(REWARD, _redeem(reward, title="IŞIK")) is not None  # I -> ı
    assert m.map(REWARD, _redeem(reward, title="işık", rid="other")) is None  # i != ı
    m2 = EventMapper(make_settings(REWARD_TITLE="İstek"))
    assert m2.map(REWARD, _redeem(reward, title="istek")) is not None  # İ -> i


def test_reward_title_mismatch(mapper, reward):
    before = _dropped("reward_title_mismatch")
    assert mapper.map(REWARD, _redeem(reward, title="Uban Request")) is None
    assert _dropped("reward_title_mismatch") == before + 1


def test_reward_rejected(mapper, reward):
    assert mapper.map(REWARD, _redeem(reward, status="rejected")) is None


def test_reward_pending_then_accepted_speaks_once(mapper, reward):
    first = mapper.map(REWARD, _redeem(reward, status="pending"))
    second = mapper.map(REWARD, _redeem(reward, status="accepted"))
    assert first is not None and first.kind == "reward"
    assert second is None


def test_reward_rejected_does_not_consume_id(mapper, reward):
    assert mapper.map(REWARD, _redeem(reward, status="rejected")) is None
    assert mapper.map(REWARD, _redeem(reward, status="accepted")) is not None


def test_reward_redemption_id_expires_after_an_hour(settings, reward):
    now = [1000.0]
    m = EventMapper(settings, clock=lambda: now[0])
    assert m.map(REWARD, _redeem(reward)) is not None
    now[0] += 3599
    assert m.map(REWARD, _redeem(reward)) is None
    now[0] += 2
    item = m.map(REWARD, _redeem(reward))
    assert item is not None and item.created_at == now[0]


def test_reward_distinct_ids_both_speak(mapper, reward):
    assert mapper.map(REWARD, _redeem(reward, rid="a")) is not None
    assert mapper.map(REWARD, _redeem(reward, rid="b")) is not None


def test_reward_empty_and_emoji_input(mapper, reward):
    assert mapper.map(REWARD, _redeem(reward, user_input="😂😂", rid="a")) is None
    assert mapper.map(REWARD, _redeem(reward, user_input="", rid="b")) is None
    p = _redeem(reward, rid="c")
    p["user_input"] = None
    assert mapper.map(REWARD, p) is None


def test_reward_null_redeemer_is_anonim(mapper, reward):
    p = _redeem(reward)
    p["redeemer"]["username"] = None
    assert mapper.map(REWARD, p).user == "anonim"
    p2 = _redeem(reward, rid="x")
    p2["redeemer"] = None
    assert mapper.map(REWARD, p2).user == "anonim"


# ---- chat.message.sent ------------------------------------------------------------------------


def test_chat_command_from_moderator(mapper, chat):
    item = mapper.map(CHAT, _chat(chat), message_id="01MSG")
    assert item is not None
    assert item.kind == "command" and item.priority == 2
    assert item.raw_text == "selam"
    assert item.user == "sender_name"
    assert "moderator" in item.meta["roles"] and item.meta["kick_message_id"] == "01MSG"


def test_chat_command_from_plain_viewer(mapper, chat):
    before = _dropped("role_denied")
    assert mapper.map(CHAT, _chat(chat, badges=[])) is None
    assert _dropped("role_denied") == before + 1


def test_chat_command_subscriber_allowed(mapper, chat):
    assert mapper.map(CHAT, _chat(chat, badges=[{"text": "Subscriber", "type": "subscriber", "count": 3}])) is not None


def test_chat_command_unlisted_role_denied(mapper, chat):
    assert mapper.map(CHAT, _chat(chat, badges=[{"text": "VIP", "type": "vip"}])) is None


def test_chat_identity_null_does_not_raise(mapper, chat):
    assert mapper.map(CHAT, _chat(chat, identity=None)) is None


def test_chat_badges_null_does_not_raise(mapper, chat):
    assert mapper.map(CHAT, _chat(chat, identity={"username_color": None, "badges": None})) is None


def test_chat_broadcaster_allowed(mapper, chat):
    p = _chat(chat, identity=None, sender_id=chat["broadcaster"]["user_id"])
    item = mapper.map(CHAT, p)
    assert item is not None and item.kind == "command"
    assert item.meta["roles"] == ["broadcaster"]


def test_chat_roles_configurable(make_settings, chat):
    m = EventMapper(make_settings(COMMAND_ROLES="vip"))
    assert m.map(CHAT, _chat(chat)) is None  # moderator no longer allowed
    assert m.map(CHAT, _chat(chat, badges=[{"type": "VIP"}])) is not None


@pytest.mark.parametrize("content", ["selam", "hello !tts", "!ttsselam", "!tt selam", "", None])
def test_chat_without_prefix_is_ignored(mapper, chat, content):
    assert mapper.map(CHAT, _chat(chat, content=content)) is None


def test_chat_bare_prefix_has_nothing_to_say(mapper, chat):
    before = _dropped("empty")
    assert mapper.map(CHAT, _chat(chat, content="!tts")) is None
    assert mapper.map(CHAT, _chat(chat, content="!tts   ")) is None
    assert _dropped("empty") == before + 2


def test_chat_prefix_case_and_whitespace(mapper, chat):
    item = mapper.map(CHAT, _chat(chat, content="  !TTS   selam abi  "))
    assert item is not None and item.raw_text == "selam abi"


def test_chat_only_emoji_command_is_dropped(mapper, chat):
    before = _dropped("empty")
    assert mapper.map(CHAT, _chat(chat, content="!tts 😂😂")) is None
    assert _dropped("empty") == before + 1


def test_chat_emote_tag_is_kept_for_the_reader(mapper, chat):
    item = mapper.map(CHAT, _chat(chat, content="!tts [emote:37226:KEKW]"))
    assert item is not None and item.raw_text == "[emote:37226:KEKW]"


def test_chat_does_not_apply_cooldown(mapper, chat):
    assert mapper.map(CHAT, _chat(chat)) is not None
    assert mapper.map(CHAT, _chat(chat)) is not None  # cooldown is the queue's job


def test_chat_custom_prefix(make_settings, chat):
    m = EventMapper(make_settings(COMMAND_PREFIX="!oku"))
    assert m.map(CHAT, _chat(chat, content="!oku merhaba")).raw_text == "merhaba"
    assert m.map(CHAT, _chat(chat, content="!tts merhaba")) is None


# ---- cross-cutting ----------------------------------------------------------------------------


def test_broadcaster_filter(make_settings, kicks, reward, chat):
    other = EventMapper(make_settings(KICK_BROADCASTER_USER_ID=42))
    before = _dropped("other_broadcaster")
    assert other.map(KICKS, kicks) is None
    assert other.map(REWARD, _redeem(reward)) is None
    assert other.map(CHAT, _chat(chat)) is None
    assert _dropped("other_broadcaster") == before + 3

    mine = EventMapper(make_settings(KICK_BROADCASTER_USER_ID=kicks["broadcaster"]["user_id"]))
    assert mine.map(KICKS, kicks) is not None
    assert mine.map(CHAT, _chat(chat)) is not None  # chat fixture has the same broadcaster id
    rmine = EventMapper(make_settings(KICK_BROADCASTER_USER_ID=reward["broadcaster"]["user_id"]))
    assert rmine.map(REWARD, _redeem(reward)) is not None


def test_broadcaster_filter_zero_accepts_any(settings, kicks):
    kicks["broadcaster"]["user_id"] = 999
    assert EventMapper(settings).map(KICKS, kicks) is not None


def test_broadcaster_filter_missing_broadcaster_is_dropped(make_settings, kicks):
    kicks["broadcaster"] = None
    assert EventMapper(make_settings(KICK_BROADCASTER_USER_ID=42)).map(KICKS, kicks) is None


def test_unknown_event_and_garbage_never_raise(mapper):
    before = _dropped("unsupported_event")
    assert mapper.map("channel.followed", {"follower": {}}) is None
    assert _dropped("unsupported_event") == before + 1
    before = _dropped("invalid_payload")
    for bad in (None, [], "x", 5, {}):
        assert mapper.map(KICKS, bad) is None
        assert mapper.map(REWARD, bad) is None
    assert _dropped("invalid_payload") > before


def test_extra_and_nulls_everywhere_are_tolerated(mapper, kicks, chat):
    kicks["surprise"] = {"new": "field"}
    kicks["broadcaster"]["identity"] = None
    kicks["gift"]["message"] = None
    assert mapper.map(KICKS, kicks) is not None
    chat["replies_to"] = None
    chat["emotes"] = None
    chat["sender"]["profile_picture"] = None
    assert mapper.map(CHAT, _chat(chat)) is not None


def test_created_at_comes_from_clock(settings, kicks):
    item = EventMapper(settings, clock=lambda: 1234.5).map(KICKS, kicks)
    assert item.created_at == 1234.5 and item.id
