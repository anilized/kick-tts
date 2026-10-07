"""Frozen contracts shared by every workstream: Reader/Engine protocols, TtsItem, WS messages."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, Protocol, Union, runtime_checkable

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

Kind = Literal["kicks", "reward", "command", "manual"]

# Lower number = spoken first. FIFO inside a priority.
PRIORITY: dict[Kind, int] = {"kicks": 0, "manual": 0, "reward": 1, "command": 2}


@runtime_checkable
class Reader(Protocol):
    """Turns raw chat text into one speakable line. "" means nothing to say. Never raises."""

    name: str

    async def read(self, text: str, user: str) -> str: ...


@runtime_checkable
class Engine(Protocol):
    """Text to PCM16 mono WAV bytes. Both methods block and run on the shared synth executor."""

    name: str

    def synth(self, text: str, sample_rate: int = 24000) -> bytes: ...

    def warmup(self) -> None: ...


@dataclass
class TtsItem:
    id: str
    kind: Kind
    user: str
    raw_text: str
    priority: int
    created_at: float
    meta: dict = field(default_factory=dict)  # amount, reward_title, kick message_id ... (logging/metrics only)

    @classmethod
    def new(
        cls,
        kind: Kind,
        user: str,
        raw_text: str,
        meta: dict | None = None,
        clock: Callable[[], float] = time.time,
    ) -> "TtsItem":
        return cls(
            id=uuid.uuid4().hex,
            kind=kind,
            user=user,
            raw_text=raw_text,
            priority=PRIORITY[kind],
            created_at=clock(),
            meta=dict(meta or {}),
        )


# ---- WebSocket messages, JSON discriminated on "type" ------------------------------------------

# server -> overlay
class OverlayItem(BaseModel):
    type: Literal["item"] = "item"
    id: str
    user: str
    caption: str
    audio_b64_wav: str
    duration_s: float


class OverlaySkip(BaseModel):
    type: Literal["skip"] = "skip"


class OverlayClear(BaseModel):
    type: Literal["clear"] = "clear"


class OverlayPaused(BaseModel):
    type: Literal["paused"] = "paused"
    value: bool


class OverlayPing(BaseModel):
    type: Literal["ping"] = "ping"


# overlay -> server
class OverlayAck(BaseModel):
    type: Literal["played"] = "played"
    id: str


class OverlayPong(BaseModel):
    type: Literal["pong"] = "pong"


ServerMessage = Annotated[
    Union[OverlayItem, OverlaySkip, OverlayClear, OverlayPaused, OverlayPing],
    Field(discriminator="type"),
]
ClientMessage = Annotated[Union[OverlayAck, OverlayPong], Field(discriminator="type")]

_client_adapter: TypeAdapter[Any] = TypeAdapter(ClientMessage)


def parse_client_message(data: Any) -> OverlayAck | OverlayPong | None:
    """Parse an overlay -> server message; None for unknown types or malformed payloads."""
    if not isinstance(data, dict):
        return None
    try:
        return _client_adapter.validate_python(data)
    except ValidationError:
        return None
