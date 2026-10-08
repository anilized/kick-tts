"""Panel routes: the page, the login/callback/logout flow and the JSON the page reads.

    GET  /panel                       the page (static HTML, relative URLs only, works under /tts)
    GET  /panel/me                    401 {detail, providers} without a session, else identity (+ keys when authorized)
    POST /panel/logout                clears the session cookie
    PUT  /panel/settings              {anthropic_api_key?, speech_speed?} runtime overrides (authorized only)
    POST /panel/settings/test-reader  one request through the current reader to validate the key
    GET  /auth/{provider}/login       302 to Kick / Discord (sets a signed login-state cookie)
    GET  /auth/{provider}/callback    exchanges the code, sets the session cookie, 302 ../../panel

Every redirect back to the page is a *relative* Location (`../../panel`), so the flow works both at
http://127.0.0.1:8000 and behind the `/tts` ingress rewrite. The only absolute URLs are the provider
endpoints and the OAuth redirect_uri, which is built from PUBLIC_BASE_URL (or the request's origin).
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app import metrics
from app.auth import LOGIN_STATE_TTL_S, AuthError, Identity, Provider, Signer, is_authorized, pkce_pair
from app.config import Settings
from app.runtime import UNSET, Runtime

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
PANEL_FILE = STATIC_DIR / "panel.html"

SESSION_COOKIE = "kicktts_session"
LOGIN_COOKIE = "kicktts_login"
PANEL_RELATIVE = "../../panel"  # from /auth/<provider>/callback

PLACEHOLDER_PANEL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>kick-tts panel (placeholder)</title></head>
<body style="margin:0;padding:24px;background:#111;color:#eee;font:16px/1.5 sans-serif">
<p>kick-tts panel: app/static/panel.html is not installed.</p>
</body></html>
"""

OBS_SOURCE_SETTINGS = {
    "width": 1920,
    "height": 1080,
    "control_audio_via_obs": True,
    "shutdown_source_when_not_visible": False,
    "refresh_browser_when_scene_becomes_active": False,
    "custom_css": "",
}


def public_base(settings: Settings, request: Request) -> str:
    """Origin (+ prefix) the browser uses: PUBLIC_BASE_URL, else the request's own scheme://host."""
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL
    return f"{request.url.scheme}://{request.url.netloc}"


def _cookie_opts(base: str) -> dict:
    path = urlsplit(base).path or "/"
    return {"httponly": True, "samesite": "lax", "secure": base.startswith("https://"), "path": path}


class SettingsUpdate(BaseModel):
    """PUT /panel/settings body. A field that is absent is left unchanged; anthropic_api_key "" or null clears
    the panel key (back to the environment value), speech_speed null resets to settings.yaml."""

    anthropic_api_key: str | None = None
    speech_speed: float | None = None


def session_payload(settings: Settings, base: str, who: Identity, runtime: Runtime | None = None) -> dict:
    """What the page shows after login. Keys are included only for authorized identities."""
    authorized = is_authorized(settings, who)
    out: dict = {"user": who.to_dict(), "authorized": authorized}
    if not authorized:
        return out
    if runtime is not None:
        out["runtime"] = runtime.describe()
    out["settings"] = {
        "base_url": base,
        "overlay_url": f"{base}/overlay?key={settings.OVERLAY_KEY}",
        "overlay_key": settings.OVERLAY_KEY,
        "control_token": settings.CONTROL_TOKEN,
        "obs": dict(OBS_SOURCE_SETTINGS),
        "channel": {
            "broadcaster_user_id": settings.KICK_BROADCASTER_USER_ID,
            "min_kicks": settings.MIN_KICKS,
            "reward_title": settings.REWARD_TITLE,
            "command_prefix": settings.COMMAND_PREFIX,
            "command_roles": settings.command_roles,
            "command_cooldown_s": settings.COMMAND_COOLDOWN_S,
            "max_text_chars": settings.MAX_TEXT_CHARS,
            "max_queue": settings.MAX_QUEUE,
            "speech_speed": runtime.speech_speed if runtime is not None else settings.SPEECH_SPEED,  # effective value
        },
    }
    return out


def build_router(settings: Settings, signer: Signer) -> APIRouter:
    router = APIRouter()

    def providers(request: Request) -> dict[str, Provider]:
        return request.app.state.providers

    def current_identity(request: Request) -> Identity | None:
        data = signer.unsign(request.cookies.get(SESSION_COOKIE))
        return Identity.from_dict(data) if data else None

    def back_to_panel(request: Request, error: str | None = None) -> RedirectResponse:
        url = PANEL_RELATIVE + (f"?error={error}" if error else "")
        return RedirectResponse(url, status_code=302)

    # -- page + JSON --------------------------------------------------------------------------------

    @router.get("/panel")
    async def panel_page() -> Response:
        if PANEL_FILE.is_file():
            return HTMLResponse(PANEL_FILE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
        return HTMLResponse(PLACEHOLDER_PANEL)

    @router.get("/panel/me")
    async def panel_me(request: Request) -> Response:
        who = current_identity(request)
        offered = sorted(providers(request))
        if who is None:
            return JSONResponse({"detail": "unauthorized", "providers": offered}, status_code=401, headers={"Cache-Control": "no-store"})
        body = session_payload(settings, public_base(settings, request), who, request.app.state.runtime)
        body["providers"] = offered
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    # -- runtime settings (Anthropic key, speech speed) --------------------------------------------

    def authorized_or_error(request: Request) -> Identity | Response:
        who = current_identity(request)
        if who is None:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        if not is_authorized(settings, who):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return who

    @router.put("/panel/settings")
    async def panel_settings_update(body: SettingsUpdate, request: Request) -> Response:
        who = authorized_or_error(request)
        if isinstance(who, Response):
            return who
        runtime: Runtime = request.app.state.runtime
        fields = body.model_fields_set
        try:
            described = await runtime.update(
                anthropic_api_key=body.anthropic_api_key if "anthropic_api_key" in fields else UNSET,
                speech_speed=body.speech_speed if "speech_speed" in fields else UNSET,
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except OSError as exc:
            log.error("panel: cannot persist settings to %s: %s", runtime.store.path, exc)
            return JSONResponse({"detail": f"could not write {runtime.store.path}: {type(exc).__name__}"}, status_code=500)
        log.info("panel: settings updated by %s %s (%s): %s", who.provider, who.id, who.name, sorted(fields))
        return JSONResponse({"status": "ok", "runtime": described}, headers={"Cache-Control": "no-store"})

    @router.post("/panel/settings/test-reader")
    async def panel_settings_test_reader(request: Request) -> Response:
        who = authorized_or_error(request)
        if isinstance(who, Response):
            return who
        result = await request.app.state.runtime.check_reader()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/panel/logout")
    async def panel_logout(request: Request) -> Response:
        resp = JSONResponse({"status": "ok"})
        resp.delete_cookie(SESSION_COOKIE, **_cookie_opts(public_base(settings, request)))
        return resp

    # -- oauth --------------------------------------------------------------------------------------

    @router.get("/auth/{provider}/login")
    async def auth_login(provider: str, request: Request) -> Response:
        prov = providers(request).get(provider)
        if prov is None:
            return JSONResponse({"detail": "unknown or unconfigured provider"}, status_code=404)
        base = public_base(settings, request)
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        redirect_uri = f"{base}/auth/{provider}/callback"
        resp = RedirectResponse(prov.authorize_url(redirect_uri, state, challenge), status_code=302)
        resp.headers["Cache-Control"] = "no-store"
        resp.set_cookie(
            LOGIN_COOKIE,
            signer.sign({"provider": provider, "state": state, "verifier": verifier}, LOGIN_STATE_TTL_S),
            max_age=int(LOGIN_STATE_TTL_S),
            **_cookie_opts(base),
        )
        return resp

    @router.get("/auth/{provider}/callback")
    async def auth_callback(provider: str, request: Request, code: str | None = None, state: str | None = None, error: str | None = None) -> Response:
        prov = providers(request).get(provider)
        if prov is None:
            return JSONResponse({"detail": "unknown or unconfigured provider"}, status_code=404)
        base = public_base(settings, request)
        opts = _cookie_opts(base)

        def fail(reason: str) -> Response:
            metrics.tts_panel_logins_total.labels(provider=provider, result="error").inc()
            resp = back_to_panel(request, reason)
            resp.delete_cookie(LOGIN_COOKIE, **opts)
            return resp

        if error:  # the user cancelled at the provider
            log.info("panel login via %s cancelled: %s", provider, error[:40])
            return fail("denied")
        pending = signer.unsign(request.cookies.get(LOGIN_COOKIE))
        if (
            not pending
            or pending.get("provider") != provider
            or not state
            or not code
            or not secrets.compare_digest(str(pending.get("state", "")), state)
        ):
            return fail("state")
        try:
            who = await prov.fetch_identity(code, f"{base}/auth/{provider}/callback", str(pending.get("verifier", "")))
        except AuthError as exc:
            log.warning("panel login via %s failed: %s", provider, exc)
            return fail("exchange")

        authorized = is_authorized(settings, who)
        metrics.tts_panel_logins_total.labels(provider=provider, result="ok" if authorized else "unauthorized").inc()
        log.info("panel login: %s user %s (%s) authorized=%s", who.provider, who.id, who.name, authorized)
        resp = back_to_panel(request)
        resp.delete_cookie(LOGIN_COOKIE, **opts)
        resp.set_cookie(
            SESSION_COOKIE,
            signer.sign(who.to_dict(), settings.PANEL_SESSION_TTL_S),
            max_age=max(1, int(settings.PANEL_SESSION_TTL_S)),
            **opts,
        )
        return resp

    return router
