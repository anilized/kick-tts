"""OverlayHub: overlay WebSocket connections, broadcast, played-ack routing, control pushes and pings.

Everything here runs on the single event loop of the service. The worker (app/worker.py) is the only
caller of `wait_ack`; routes call the `push_*` helpers; the `/ws` route hands each socket to `serve`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from app import metrics
from app.interfaces import (
    OverlayAck,
    OverlayClear,
    OverlayPaused,
    OverlayPing,
    OverlayPong,
    OverlaySkip,
    parse_client_message,
)

log = logging.getLogger(__name__)

CLOSE_GOING_AWAY = 1001
CLOSE_POLICY_VIOLATION = 1008


class OverlayHub:
    """Fan-out to every connected overlay plus the pacing primitive the worker waits on."""

    def __init__(self, ping_interval_s: float = 20.0) -> None:
        self._ping_interval_s = ping_interval_s
        self._clients: set[WebSocket] = set()
        self._acks: dict[str, asyncio.Future[bool]] = {}
        self._has_clients = asyncio.Event()

    # -- connections -----------------------------------------------------------------------------

    @property
    def clients(self) -> int:
        return len(self._clients)

    def _sync_gauge(self) -> None:
        metrics.tts_overlay_clients.set(len(self._clients))

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        self._has_clients.set()
        self._sync_gauge()
        log.info("overlay connected (%d clients)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        if ws not in self._clients:
            return
        self._clients.discard(ws)
        self._sync_gauge()
        log.info("overlay disconnected (%d clients)", len(self._clients))
        if not self._clients:
            self._has_clients.clear()
            # Nobody is left to play (and ack) the current item: let the worker move on.
            self.release_acks()

    async def wait_for_client(self) -> None:
        """Block until at least one overlay is connected."""
        await self._has_clients.wait()

    async def serve(self, ws: WebSocket) -> None:
        """Accept `ws`, then route its messages until it disconnects. Unknown messages are ignored."""
        await self.connect(ws)
        try:
            while True:
                try:
                    text = await ws.receive_text()
                except (WebSocketDisconnect, RuntimeError):
                    break
                self._on_client_message(text)
        finally:
            self.disconnect(ws)

    def _on_client_message(self, text: str) -> None:
        try:
            data: Any = json.loads(text)
        except (TypeError, ValueError):
            return
        msg = parse_client_message(data)
        if isinstance(msg, OverlayAck):
            self.ack(msg.id)
        elif isinstance(msg, OverlayPong):
            pass
        # anything else: ignored

    async def close_all(self, code: int = CLOSE_GOING_AWAY) -> None:
        for ws in list(self._clients):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.close(code=code)
            except Exception:  # already gone
                pass
            self.disconnect(ws)

    # -- broadcast -------------------------------------------------------------------------------

    async def broadcast(self, message: BaseModel) -> int:
        """Send `message` to every client; sockets that fail to send are pruned. Returns the send count."""
        payload = message.model_dump_json()
        sent = 0
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
                sent += 1
            except Exception as exc:
                log.warning("overlay send failed (%s); dropping client", type(exc).__name__)
                self.disconnect(ws)
        return sent

    async def ping_loop(self) -> None:
        """App-level ping every WS_PING_INTERVAL_S; `broadcast` prunes sockets that fail to send."""
        while True:
            await asyncio.sleep(self._ping_interval_s)
            if self._clients:
                await self.broadcast(OverlayPing())

    # -- pacing ----------------------------------------------------------------------------------

    def expect_ack(self, item_id: str) -> "asyncio.Future[bool]":
        """Register interest in the played-ack for `item_id` BEFORE broadcasting it, so an ack that
        arrives from a fast client while the item is still being sent to the others is not lost."""
        fut = self._acks.get(item_id)
        if fut is None:  # an already-resolved future (early ack) must be kept, not replaced
            fut = asyncio.get_running_loop().create_future()
            self._acks[item_id] = fut
        return fut

    def discard_ack(self, item_id: str) -> None:
        fut = self._acks.pop(item_id, None)
        if fut is not None and not fut.done():
            fut.cancel()

    async def wait_ack(self, item_id: str, timeout_s: float) -> bool:
        """True when an overlay reported `item_id` played; False on timeout or when released early.

        Uses the future registered by `expect_ack` when there is one, otherwise registers it now."""
        fut = self.expect_ack(item_id)
        if not self._clients and not fut.done():
            self.discard_ack(item_id)
            return False
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError:
            return False
        finally:
            if self._acks.get(item_id) is fut:
                del self._acks[item_id]

    def ack(self, item_id: str) -> None:
        fut = self._acks.get(item_id)
        if fut is not None and not fut.done():
            fut.set_result(True)

    def release_acks(self) -> None:
        """Resolve every pending ack wait (skip, or the last client left)."""
        for fut in list(self._acks.values()):
            if not fut.done():
                fut.set_result(False)

    # -- control pushes --------------------------------------------------------------------------

    async def push_skip(self) -> None:
        await self.broadcast(OverlaySkip())
        self.release_acks()

    async def push_clear(self) -> None:
        await self.broadcast(OverlayClear())
        self.release_acks()

    async def push_paused(self, value: bool) -> None:
        await self.broadcast(OverlayPaused(value=value))
