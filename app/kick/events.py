"""Kick webhook payload models (docs/kick/event-types.md) and the event -> TtsItem mapper.

Every model ignores unknown keys and tolerates the nulls Kick sends (identity, anonymous gifters).
The mapper dispatches on the Kick-Event-Type string, never raises, and counts every drop.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.config import Settings
from app.interfaces import TtsItem
from app.kick.dedupe import TtlSet
from app.metrics import tts_items_dropped_total
from app.reader.emoji import is_only_emoji

log = logging.getLogger(__name__)

ANONYMOUS_USER = "anonim"

EVENT_CHAT = "chat.message.sent"
EVENT_KICKS = "kicks.gifted"
EVENT_REWARD = "channel.reward.redemption.updated"

REDEMPTION_TTL_S = 3600.0


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Badge(_Model):
    text: str | None = None
    type: str | None = None
    count: int | None = None


class Identity(_Model):
    username_color: str | None = None
    badges: list[Badge] = []

    @field_validator("badges", mode="before")
    @classmethod
    def _null_badges(cls, v: Any) -> Any:
        return [] if v is None else v


class KickUser(_Model):
    is_anonymous: bool | None = None
    user_id: int | None = None
    username: str | None = None
    is_verified: bool | None = None
    profile_picture: str | None = None
    channel_slug: str | None = None
    identity: Identity | None = None


class ChatMessageEvent(_Model):
    message_id: str | None = None
    broadcaster: KickUser | None = None
    sender: KickUser | None = None
    content: str | None = None


class Gift(_Model):
    amount: int
    name: str | None = None
    type: str | None = None
    tier: str | None = None
    message: str | None = None
    pinned_time_seconds: int | None = None


class KicksGiftedEvent(_Model):
    broadcaster: KickUser | None = None
    sender: KickUser | None = None
    gift: Gift
    created_at: str | None = None


class Reward(_Model):
    id: str | None = None
    title: str | None = None
    cost: int | None = None
    description: str | None = None


class RedemptionEvent(_Model):
    id: str
    user_input: str | None = None
    status: str | None = None
    redeemed_at: str | None = None
    reward: Reward | None = None
    redeemer: KickUser | None = None
    broadcaster: KickUser | None = None


def _tr_fold(s: str) -> str:
    """Case-insensitive comparison key with Turkish dotted/dotless I handling."""
    return s.replace("İ", "i").replace("I", "ı").lower().casefold().strip()


def _drop(reason: str) -> None:
    tts_items_dropped_total.labels(reason=reason).inc()
    return None


class EventMapper:
    """Maps a Kick webhook payload to a TtsItem, or None when nothing should be spoken."""

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self._s = settings
        self._clock = clock
        self._redemptions = TtlSet(REDEMPTION_TTL_S, clock=clock)
        self._statuses = set(settings.reward_statuses)
        self._roles = set(settings.command_roles)

    def map(self, event_type: str, payload: Any, message_id: str | None = None) -> TtsItem | None:
        """`message_id` is the Kick-Event-Message-Id header, recorded in meta when the caller has it."""
        try:
            return self._map(event_type, payload, message_id)
        except ValidationError:
            return _drop("invalid_payload")
        except Exception:  # the webhook handler must always be able to ack
            log.exception("kick event mapping failed for %s", event_type)
            return _drop("invalid_payload")

    # -- dispatch ---------------------------------------------------------------------------

    def _map(self, event_type: str, payload: Any, message_id: str | None) -> TtsItem | None:
        if not isinstance(payload, dict):
            return _drop("invalid_payload")
        if event_type == EVENT_KICKS:
            return self._kicks(KicksGiftedEvent.model_validate(payload), message_id)
        if event_type == EVENT_REWARD:
            return self._reward(RedemptionEvent.model_validate(payload), message_id)
        if event_type == EVENT_CHAT:
            return self._chat(ChatMessageEvent.model_validate(payload), message_id)
        return _drop("unsupported_event")

    def _other_broadcaster(self, broadcaster: KickUser | None) -> bool:
        wanted = self._s.KICK_BROADCASTER_USER_ID
        return bool(wanted) and (broadcaster is None or broadcaster.user_id != wanted)

    def _item(self, kind, user: str, text: str, meta: dict, message_id: str | None) -> TtsItem | None:
        if not text.strip() or is_only_emoji(text):
            return _drop("empty")
        if message_id:
            meta["kick_message_id"] = message_id
        return TtsItem.new(kind, user, text, meta=meta, clock=self._clock)

    # -- kicks.gifted -----------------------------------------------------------------------

    def _kicks(self, ev: KicksGiftedEvent, message_id: str | None) -> TtsItem | None:
        if self._other_broadcaster(ev.broadcaster):
            return _drop("other_broadcaster")
        amount = ev.gift.amount
        if amount < self._s.MIN_KICKS:
            return _drop("below_min_kicks")
        sender = ev.sender
        anonymous = sender is None or bool(sender.is_anonymous) or not (sender.username or "").strip()
        user = ANONYMOUS_USER if anonymous else sender.username.strip()
        text = f"{user} {amount} kick gönderdi"
        note = (ev.gift.message or "").strip()
        if note and not is_only_emoji(note):
            text += f": {note}"
        meta = {"amount": amount, "gift_name": ev.gift.name}
        return self._item("kicks", user, text, meta, message_id)

    # -- channel.reward.redemption.updated --------------------------------------------------

    def _reward(self, ev: RedemptionEvent, message_id: str | None) -> TtsItem | None:
        if self._other_broadcaster(ev.broadcaster):
            return _drop("other_broadcaster")
        title = (ev.reward.title if ev.reward else None) or ""
        if _tr_fold(title) != _tr_fold(self._s.REWARD_TITLE):
            return _drop("reward_title_mismatch")
        status = (ev.status or "").strip().lower()
        if status not in self._statuses:
            return _drop("reward_status")
        text = (ev.user_input or "").strip()
        if not text or is_only_emoji(text):
            return _drop("empty")
        if self._redemptions.seen_or_add(ev.id):  # pending -> accepted must speak once
            return _drop("duplicate_redemption")
        user = (ev.redeemer.username if ev.redeemer else None) or ANONYMOUS_USER
        meta = {"reward_title": title, "redemption_id": ev.id, "status": status}
        return self._item("reward", user, text, meta, message_id)

    # -- chat.message.sent ------------------------------------------------------------------

    def _chat(self, ev: ChatMessageEvent, message_id: str | None) -> TtsItem | None:
        prefix = self._s.COMMAND_PREFIX
        content = (ev.content or "").strip()
        head, rest = content[: len(prefix)], content[len(prefix) :]
        if not prefix or _tr_fold(head) != _tr_fold(prefix) or (rest and not rest[0].isspace()):
            return _drop("not_command")
        if self._other_broadcaster(ev.broadcaster):
            return _drop("other_broadcaster")

        sender = ev.sender
        roles: set[str] = set()
        if sender is not None:
            if sender.identity is not None:
                roles.update((b.type or "").lower() for b in sender.identity.badges)
            if (
                sender.user_id is not None
                and ev.broadcaster is not None
                and sender.user_id == ev.broadcaster.user_id
            ):
                roles.add("broadcaster")
        if not roles & self._roles:
            return _drop("role_denied")

        text = rest.strip()
        anonymous = sender is None or bool(sender.is_anonymous) or not (sender.username or "").strip()
        user = ANONYMOUS_USER if anonymous else sender.username.strip()
        meta = {"roles": sorted(r for r in roles if r), "chat_message_id": ev.message_id}
        return self._item("command", user, text, meta, message_id)
