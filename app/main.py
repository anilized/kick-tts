"""kick-tts FastAPI service: Kick webhook -> queue -> worker -> overlay WebSocket.

This module must never import torch. The default engine comes from `app.engine.get_engine`, which is
resolved lazily inside the warm-up task on the synth executor thread (EmaEngine's constructor imports
torch there, after it has set the OMP/MKL env). Lifespan yields immediately, so /healthz, /readyz and the
webhook are reachable while the model is still loading.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import httpx
from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from app import metrics
from app.auth import HTTP_TIMEOUT, Signer, build_providers
from app.config import Settings
from app.engine import get_engine
from app.interfaces import Engine, Reader, TtsItem
from app.kick.dedupe import TtlSet
from app.kick.events import EventMapper
from app.kick.signature import KeyProvider, PublicKeyCache, http_key_provider
from app.panel import build_router as build_panel_router
from app.queue import TtsQueue
from app.reader import get_reader
from app.reader.emoji import is_only_emoji
from app.runtime import Runtime, RuntimeStore
from app.worker import Worker
from app.ws import CLOSE_POLICY_VIOLATION, OverlayHub

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
OVERLAY_FILE = STATIC_DIR / "overlay.html"

MESSAGE_ID_TTL_S = 600.0

H_MESSAGE_ID = "Kick-Event-Message-Id"
H_SUBSCRIPTION_ID = "Kick-Event-Subscription-Id"
H_SIGNATURE = "Kick-Event-Signature"
H_TIMESTAMP = "Kick-Event-Message-Timestamp"
H_TYPE = "Kick-Event-Type"
H_VERSION = "Kick-Event-Version"
KICK_HEADERS = (H_MESSAGE_ID, H_SUBSCRIPTION_ID, H_SIGNATURE, H_TIMESTAMP, H_TYPE, H_VERSION)

# Served when TASK-107's app/static/overlay.html is not present. Relative URLs only.
PLACEHOLDER_OVERLAY = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>TTS overlay (placeholder)</title></head>
<body style="margin:0;background:transparent;color:#fff;font:700 32px/1.3 sans-serif">
<div id="caption">🔊 TTS · overlay.html is not installed yet (app/static/overlay.html)</div>
</body></html>
"""


def _static_key_provider(pem: str) -> KeyProvider:
    async def provider() -> str:
        return pem

    return provider


def _ct_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class SpeakBody(BaseModel):
    text: str
    user: str = "manual"


def create_app(
    settings: Settings,
    engine: Engine | None = None,
    reader: Reader | None = None,
    clock: Callable[[], float] | None = None,
    key_provider: KeyProvider | None = None,
    oauth_http: httpx.AsyncClient | None = None,
    reader_factory: Callable[[Settings], Reader] | None = None,
) -> FastAPI:
    clock = clock or time.time
    logging.getLogger("app").setLevel(settings.LOG_LEVEL.upper())

    if key_provider is None:
        if settings.KICK_PUBLIC_KEY_PEM:
            key_provider = _static_key_provider(settings.KICK_PUBLIC_KEY_PEM)
        else:
            key_provider = http_key_provider(settings.KICK_PUBLIC_KEY_URL)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        state = app.state
        state.settings = settings
        state.readiness = "warming"
        state.readiness_error = None
        state.ready = asyncio.Event()
        state.hub = OverlayHub(settings.WS_PING_INTERVAL_S)
        state.queue = TtsQueue(settings.MAX_QUEUE, settings.COMMAND_COOLDOWN_S, settings.MAX_ITEM_AGE_S, clock=clock)
        state.mapper = EventMapper(settings, clock=clock)
        state.seen_ids = TtlSet(MESSAGE_ID_TTL_S, clock=clock)
        state.key_cache = PublicKeyCache(key_provider)
        state.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-synth")
        state.engine = engine
        state.runtime = Runtime(settings, RuntimeStore(settings.PANEL_STATE_PATH), reader_factory or get_reader, reader)
        state.reader = state.runtime.reader  # kept for /status; the worker reads state.runtime.reader
        state.oauth_http = oauth_http if oauth_http is not None else httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        state.providers = build_providers(settings, state.oauth_http)

        def build_and_warm() -> Engine:
            eng = engine if engine is not None else get_engine(settings)  # EmaEngine: env, torch, weights
            eng.warmup()
            return eng

        async def warm() -> None:
            try:
                eng = await loop.run_in_executor(state.executor, build_and_warm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.readiness = "failed"
                state.readiness_error = type(exc).__name__
                metrics.tts_failures_total.labels(stage="warmup").inc()
                log.error("engine warm-up failed: %s: %s", type(exc).__name__, exc, exc_info=True)
                return
            state.engine = eng
            state.runtime.attach_engine(eng)  # panel speed override, if any
            state.readiness = "ready"
            state.ready.set()
            log.info("engine %s ready", getattr(eng, "name", "?"))

        state.worker = Worker(
            settings,
            state.queue,
            state.hub,
            lambda: state.runtime.reader,
            lambda: state.engine,
            state.executor,
            state.ready,
            polish_getter=lambda: state.runtime.polish,
        )
        state.warmup_task = asyncio.create_task(warm(), name="tts-warmup")
        state.worker_task = asyncio.create_task(state.worker.run(), name="tts-worker")
        state.ping_task = asyncio.create_task(state.hub.ping_loop(), name="tts-ping")
        log.info("kick-tts started (warm-up in background)")
        try:
            yield
        finally:
            tasks = [state.warmup_task, state.worker_task, state.ping_task]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await state.hub.close_all()
            if oauth_http is None:
                await state.oauth_http.aclose()
            state.executor.shutdown(wait=False, cancel_futures=True)
            log.info("kick-tts stopped")

    app = FastAPI(title="kick-tts", lifespan=lifespan, redirect_slashes=False, docs_url=None, redoc_url=None)
    app.include_router(build_panel_router(settings, Signer.from_settings(settings)))

    # -- auth -------------------------------------------------------------------------------------

    def require_bearer(request: Request) -> None:
        auth = request.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        ok = scheme.lower() == "bearer" and _ct_equal(token.strip(), settings.CONTROL_TOKEN)
        if not ok:
            raise _Unauthorized()

    def overlay_key_ok(key: str | None) -> bool:
        return key is not None and _ct_equal(key, settings.OVERLAY_KEY)

    class _Unauthorized(Exception):
        pass

    @app.exception_handler(_Unauthorized)
    async def _unauthorized_handler(request: Request, exc: _Unauthorized) -> Response:
        return JSONResponse({"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})

    # -- health -----------------------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz(request: Request) -> Response:
        st = request.app.state
        if st.readiness == "ready":
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": st.readiness, "error": st.readiness_error}, status_code=503)

    @app.get("/metrics")
    async def prom_metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # -- overlay ----------------------------------------------------------------------------------

    @app.get("/overlay")
    async def overlay(key: str | None = None) -> Response:
        if not overlay_key_ok(key):
            return PlainTextResponse("forbidden", status_code=403)
        if OVERLAY_FILE.is_file():
            return HTMLResponse(OVERLAY_FILE.read_text(encoding="utf-8"))
        return HTMLResponse(PLACEHOLDER_OVERLAY)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, key: str | None = None) -> None:
        if not overlay_key_ok(key):
            await ws.accept()
            await ws.close(code=CLOSE_POLICY_VIOLATION, reason="bad key")
            return
        await ws.app.state.hub.serve(ws)

    # -- kick webhook -----------------------------------------------------------------------------

    @app.post("/webhook/kick")
    async def webhook_kick(request: Request) -> Response:
        st = request.app.state
        raw = await request.body()  # signature is over the raw bytes, never a re-serialization
        headers = {name: request.headers.get(name) for name in KICK_HEADERS}
        if any(not v for v in headers.values()):
            metrics.tts_webhook_rejected_total.labels(reason="missing_headers").inc()
            return JSONResponse({"detail": "missing Kick-Event-* headers"}, status_code=400)
        message_id = headers[H_MESSAGE_ID]
        ok = await st.key_cache.verify(message_id, headers[H_TIMESTAMP], raw, headers[H_SIGNATURE])
        if not ok:
            metrics.tts_webhook_rejected_total.labels(reason="bad_signature").inc()
            return JSONResponse({"detail": "invalid signature"}, status_code=401)
        if st.seen_ids.seen_or_add(message_id):
            return JSONResponse({"status": "duplicate"})
        event_type = headers[H_TYPE]
        metrics.tts_events_received_total.labels(type=event_type).inc()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None  # the mapper counts it as invalid_payload
        item = st.mapper.map(event_type, payload, message_id=message_id)
        if item is None:
            return JSONResponse({"status": "ignored"})
        result = st.queue.put(item)
        if not result.accepted:
            return JSONResponse({"status": "dropped", "reason": result.reason})
        return JSONResponse({"status": "queued", "id": item.id})

    # -- control API ------------------------------------------------------------------------------

    @app.post("/speak", dependencies=[Depends(require_bearer)])
    async def speak(body: SpeakBody, request: Request) -> Response:
        st = request.app.state
        text = body.text.strip()
        if not text or is_only_emoji(text):
            metrics.tts_items_dropped_total.labels(reason="empty").inc()
            return JSONResponse({"status": "ignored", "reason": "empty"})
        user = body.user.strip() or "manual"
        item = TtsItem.new("manual", user, text, clock=clock)
        result = st.queue.put(item)
        if not result.accepted:
            return JSONResponse({"status": "dropped", "reason": result.reason})
        return JSONResponse({"status": "queued", "id": item.id, "queue_length": len(st.queue)})

    @app.post("/skip", dependencies=[Depends(require_bearer)])
    async def skip(request: Request) -> dict:
        await request.app.state.hub.push_skip()
        return {"status": "ok"}

    @app.post("/clear", dependencies=[Depends(require_bearer)])
    async def clear(request: Request) -> dict:
        st = request.app.state
        n = st.queue.clear()
        await st.hub.push_clear()
        return {"status": "ok", "cleared": n}

    @app.post("/pause", dependencies=[Depends(require_bearer)])
    async def pause(request: Request) -> dict:
        st = request.app.state
        st.queue.pause()
        await st.hub.push_paused(True)
        return {"status": "ok", "paused": True}

    @app.post("/resume", dependencies=[Depends(require_bearer)])
    async def resume(request: Request) -> dict:
        st = request.app.state
        st.queue.resume()
        await st.hub.push_paused(False)
        return {"status": "ok", "paused": False}

    @app.get("/status", dependencies=[Depends(require_bearer)])
    async def status(request: Request) -> dict:
        st = request.app.state
        return {
            "queue_length": len(st.queue),
            "paused": st.queue.paused,
            "clients": st.hub.clients,
            "readiness": st.readiness,
            "error": st.readiness_error,
            "engine": getattr(st.engine, "name", None),
            "reader": getattr(st.runtime.reader, "name", None),
        }

    return app


app = create_app(Settings())
