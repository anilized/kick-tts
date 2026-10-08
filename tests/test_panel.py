"""Panel: static page checks, signed cookies, Kick/Discord login flow with a mocked provider, key delivery.

Offline: the provider endpoints are served by an httpx.MockTransport injected through create_app(oauth_http=...).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import Identity, Signer, allowed_entries, is_authorized, pkce_pair
from app.main import create_app
from app.metrics import get_value, tts_panel_logins_total

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "app" / "static" / "panel.html"

KICK_USER = {"user_id": 4242, "name": "anildev", "email": "x@example.com", "profile_picture": "https://kick.com/img/a.webp"}
DISCORD_USER = {"id": "123456789012345678", "username": "anil", "global_name": "Anil", "avatar": "abc123"}


# ---- provider mock -----------------------------------------------------------------------------------


class FakeProviders:
    """Serves id.kick.com / api.kick.com / discord.com. Records the token-request forms for assertions."""

    def __init__(self) -> None:
        self.token_forms: list[dict[str, str]] = []
        self.fail_token = False
        self.kick_user = dict(KICK_USER)
        self.discord_user = dict(DISCORD_USER)

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if (host, path) in {("id.kick.com", "/oauth/token"), ("discord.com", "/api/oauth2/token")}:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append(form)
            if self.fail_token:
                return httpx.Response(401, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "provider-token-" + host, "token_type": "Bearer", "expires_in": 7200})
        if request.headers.get("Authorization") != "Bearer provider-token-" + {"api.kick.com": "id.kick.com"}.get(host, host):
            return httpx.Response(401, json={"message": "bad token"})
        if (host, path) == ("api.kick.com", "/public/v1/users"):
            return httpx.Response(200, json={"data": [self.kick_user], "message": "ok"})
        if (host, path) == ("discord.com", "/api/users/@me"):
            return httpx.Response(200, json=self.discord_user)
        return httpx.Response(404, json={"message": f"unexpected {host}{path}"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def providers() -> FakeProviders:
    return FakeProviders()


@pytest.fixture
def make_client(make_settings, fake_engine, fake_reader, providers):
    clients: list[TestClient] = []

    def _make(**overrides) -> TestClient:
        settings = make_settings(
            KICK_CLIENT_ID="kick-id", KICK_CLIENT_SECRET="kick-secret",
            DISCORD_CLIENT_ID="discord-id", DISCORD_CLIENT_SECRET="discord-secret",
            **overrides,
        )
        app = create_app(settings, engine=fake_engine, reader=fake_reader, oauth_http=providers.client())
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def _login(c: TestClient, provider: str, code: str = "the-code") -> tuple[httpx.Response, dict[str, list[str]]]:
    """Run /auth/<p>/login then the callback with the state the server chose. Returns (callback response, authorize query)."""
    r = c.get(f"/auth/{provider}/login", follow_redirects=False)
    assert r.status_code == 302, r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    cb = c.get(f"/auth/{provider}/callback", params={"code": code, "state": query["state"][0]}, follow_redirects=False)
    return cb, query


# ---- static page -------------------------------------------------------------------------------------


def test_panel_html_is_self_contained_and_relative():
    html = PANEL.read_text(encoding="utf-8")
    assert PANEL.is_file() and "<html" in html
    for needle in ("http://", "https://"):
        assert needle not in html, needle
    assert not re.search(r"""['"]/[A-Za-z]""", html), "absolute path literal found"
    assert not re.search(r"<script[^>]*\bsrc\s*=", html, re.I)
    assert not re.search(r"<link[^>]*\bhref\s*=", html, re.I)
    assert "@import" not in html
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", html)
    # relative API paths and login links, as resolved from /panel (or /tts/panel)
    for token in ("'panel/me'", "'panel/logout'", "'auth/{p}/login'", "'status'", "'speak'"):
        assert token in html, token
    assert "Control audio via OBS" in html


def test_panel_page_served_without_auth(make_client):
    c = make_client()
    r = c.get("/panel")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert r.text == PANEL.read_text(encoding="utf-8")
    assert c.get("/panel/", follow_redirects=False).status_code == 404  # nothing redirects


# ---- signer ------------------------------------------------------------------------------------------


def test_signer_round_trip_tamper_and_expiry():
    s = Signer(b"0123456789abcdef0123456789abcdef")
    tok = s.sign({"a": 1, "provider": "kick"}, ttl_s=60, now=1000.0)
    assert s.unsign(tok, now=1059.0) == {"a": 1, "provider": "kick", "exp": 1060.0}
    assert s.unsign(tok, now=1060.0) is None  # expired
    body, _, sig = tok.rpartition(".")
    assert s.unsign(body + "." + sig[:-2] + "AA", now=1000.0) is None  # bad signature
    forged = base64.urlsafe_b64encode(b'{"a":2,"exp":9e12}').rstrip(b"=").decode() + "." + sig
    assert s.unsign(forged, now=1000.0) is None
    assert s.unsign(None) is None and s.unsign("") is None and s.unsign("nodot") is None
    assert Signer(b"another-key-another-key-another").unsign(tok, now=1000.0) is None
    with pytest.raises(ValueError):
        Signer(b"short")


def test_signer_derives_from_control_token_when_no_session_secret(make_settings):
    a = Signer.from_settings(make_settings(CONTROL_TOKEN="control-token-aaaaaaaaaaaaaaaaaaaa"))
    b = Signer.from_settings(make_settings(CONTROL_TOKEN="control-token-aaaaaaaaaaaaaaaaaaaa"))
    other = Signer.from_settings(make_settings(CONTROL_TOKEN="control-token-bbbbbbbbbbbbbbbbbbbb"))
    explicit = Signer.from_settings(make_settings(CONTROL_TOKEN="control-token-aaaaaaaaaaaaaaaaaaaa", SESSION_SECRET="s" * 32))
    tok = a.sign({"x": 1}, 60)
    assert b.unsign(tok) == a.unsign(tok)
    assert other.unsign(tok) is None and explicit.unsign(tok) is None


def test_pkce_pair_is_s256():
    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and 43 <= len(verifier) <= 128


# ---- allow list --------------------------------------------------------------------------------------


def test_allowed_entries_and_is_authorized(make_settings, caplog):
    s = make_settings(PANEL_ALLOWED_USERS=" kick:AnilDev, discord:123 ,bogus, twitch:x ,kick:", KICK_BROADCASTER_USER_ID=777)
    assert allowed_entries(s) == {("kick", "anildev"), ("discord", "123")}
    assert is_authorized(s, Identity("kick", "777", "someone"))  # the broadcaster, by id
    assert is_authorized(s, Identity("kick", "1", "anildev"))  # by name, case-insensitive
    assert is_authorized(s, Identity("discord", "123", "whoever"))
    assert not is_authorized(s, Identity("discord", "124", "anildev"))  # name entry is provider-scoped
    assert not is_authorized(s, Identity("kick", "778", "viewer"))
    assert not is_authorized(make_settings(KICK_BROADCASTER_USER_ID=0), Identity("kick", "0", "zero"))


# ---- login flow --------------------------------------------------------------------------------------


def test_me_without_session_lists_offered_providers(make_client):
    c = make_client()
    r = c.get("/panel/me")
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized", "providers": ["discord", "kick"]}
    assert r.headers["cache-control"] == "no-store"


def test_unconfigured_provider_is_404(make_settings, fake_engine, fake_reader, providers):
    settings = make_settings(KICK_CLIENT_ID="kick-id", KICK_CLIENT_SECRET="kick-secret")  # no Discord
    app = create_app(settings, engine=fake_engine, reader=fake_reader, oauth_http=providers.client())
    with TestClient(app) as c:
        assert c.get("/panel/me").json()["providers"] == ["kick"]
        assert c.get("/auth/discord/login", follow_redirects=False).status_code == 404
        assert c.get("/auth/discord/callback?code=a&state=b", follow_redirects=False).status_code == 404
        assert c.get("/auth/twitch/login", follow_redirects=False).status_code == 404
        assert c.get("/auth/kick/login", follow_redirects=False).status_code == 302


def test_kick_login_redirect_uses_pkce_and_sets_state_cookie(make_client):
    c = make_client()
    r = c.get("/auth/kick/login", follow_redirects=False)
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    assert (url.scheme, url.netloc, url.path) == ("https", "id.kick.com", "/oauth/authorize")
    q = parse_qs(url.query)
    assert q["response_type"] == ["code"] and q["client_id"] == ["kick-id"] and q["scope"] == ["user:read"]
    assert q["redirect_uri"] == ["http://testserver/auth/kick/callback"]
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) == 43 and len(q["state"][0]) >= 24
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("kicktts_login=") and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie  # http base -> no Secure flag (local dev)
    assert r.headers["cache-control"] == "no-store"


def test_public_base_url_drives_redirect_uri_and_cookie_path(make_client):
    c = make_client(PUBLIC_BASE_URL="https://anildev.io/tts/")
    r = c.get("/auth/discord/login", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["redirect_uri"] == ["https://anildev.io/tts/auth/discord/callback"]
    assert q["scope"] == ["identify"] and q["client_id"] == ["discord-id"]
    cookie = r.headers["set-cookie"]
    assert "Path=/tts" in cookie and "Secure" in cookie


def test_kick_full_flow_broadcaster_gets_keys(make_client, providers):
    before_ok = get_value(tts_panel_logins_total, provider="kick", result="ok")
    c = make_client(KICK_BROADCASTER_USER_ID=4242)
    cb, query = _login(c, "kick")
    assert cb.status_code == 302 and cb.headers["location"] == "../../panel"  # relative: works under /tts too
    cookies = cb.headers.get_list("set-cookie")
    assert any(ck.startswith("kicktts_session=") and "HttpOnly" in ck for ck in cookies)
    assert any(ck.startswith("kicktts_login=") and ("Max-Age=0" in ck or "expires" in ck.lower()) for ck in cookies)

    # the token exchange carried the code, the redirect_uri and a verifier matching the challenge we were sent
    (form,) = providers.token_forms
    assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
    assert form["client_id"] == "kick-id" and form["client_secret"] == "kick-secret"
    assert form["redirect_uri"] == "http://testserver/auth/kick/callback"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == query["code_challenge"][0]

    me = c.get("/panel/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"] == {"provider": "kick", "id": "4242", "name": "anildev", "avatar": "https://kick.com/img/a.webp"}
    assert body["authorized"] is True and body["providers"] == ["discord", "kick"]
    s = body["settings"]
    assert s["overlay_key"] == "test-key" and s["control_token"] == "test-token"
    assert s["overlay_url"] == "http://testserver/overlay?key=test-key" and s["base_url"] == "http://testserver"
    assert s["obs"]["width"] == 1920 and s["obs"]["height"] == 1080 and s["obs"]["control_audio_via_obs"] is True
    assert s["channel"]["broadcaster_user_id"] == 4242 and s["channel"]["command_roles"] == ["broadcaster", "moderator", "subscriber"]
    assert get_value(tts_panel_logins_total, provider="kick", result="ok") == before_ok + 1

    # the overlay URL the panel hands out really opens the overlay, and the token really drives the control API
    assert c.get(f"/overlay?key={s['overlay_key']}").status_code == 200
    assert c.get("/status", headers={"Authorization": f"Bearer {s['control_token']}"}).status_code == 200

    # logout clears the session
    r = c.post("/panel/logout")
    assert r.status_code == 200 and "kicktts_session=" in r.headers["set-cookie"]
    assert c.get("/panel/me").status_code == 401


def test_discord_full_flow_allowlisted_by_id(make_client, providers):
    c = make_client(PANEL_ALLOWED_USERS="discord:123456789012345678")
    cb, _ = _login(c, "discord")
    assert cb.status_code == 302
    (form,) = providers.token_forms
    assert form["client_id"] == "discord-id" and form["client_secret"] == "discord-secret"
    assert form["redirect_uri"] == "http://testserver/auth/discord/callback" and "code_verifier" not in form
    body = c.get("/panel/me").json()
    assert body["user"]["provider"] == "discord" and body["user"]["id"] == "123456789012345678"
    assert body["user"]["name"] == "Anil"  # global_name preferred
    assert body["user"]["avatar"] == "https://cdn.discordapp.com/avatars/123456789012345678/abc123.png?size=128"
    assert body["authorized"] is True and body["settings"]["control_token"] == "test-token"


def test_logged_in_but_not_allowed_gets_no_keys(make_client, providers):
    before = get_value(tts_panel_logins_total, provider="discord", result="unauthorized")
    c = make_client(PANEL_ALLOWED_USERS="kick:anildev", KICK_BROADCASTER_USER_ID=4242)
    cb, _ = _login(c, "discord")
    assert cb.status_code == 302 and cb.headers["location"] == "../../panel"
    body = c.get("/panel/me").json()
    assert body["authorized"] is False and "settings" not in body
    assert body["user"]["provider"] == "discord"
    assert "test-token" not in json.dumps(body) and "test-key" not in json.dumps(body)
    assert get_value(tts_panel_logins_total, provider="discord", result="unauthorized") == before + 1


def test_callback_rejects_bad_state_missing_cookie_and_provider_mismatch(make_client, providers):
    c = make_client(KICK_BROADCASTER_USER_ID=4242)
    # no login cookie at all
    r = c.get("/auth/kick/callback", params={"code": "x", "state": "y"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "../../panel?error=state"
    # wrong state
    r0 = c.get("/auth/kick/login", follow_redirects=False)
    r = c.get("/auth/kick/callback", params={"code": "x", "state": "not-the-state"}, follow_redirects=False)
    assert r.headers["location"] == "../../panel?error=state"
    # login started with kick, callback hits discord
    r0 = c.get("/auth/kick/login", follow_redirects=False)
    state = parse_qs(urlsplit(r0.headers["location"]).query)["state"][0]
    r = c.get("/auth/discord/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert r.headers["location"] == "../../panel?error=state"
    # provider-side cancel
    r = c.get("/auth/kick/callback", params={"error": "access_denied", "state": state}, follow_redirects=False)
    assert r.headers["location"] == "../../panel?error=denied"
    assert providers.token_forms == []  # never talked to the provider
    assert c.get("/panel/me").status_code == 401


def test_callback_when_provider_rejects_the_code(make_client, providers):
    before = get_value(tts_panel_logins_total, provider="kick", result="error")
    providers.fail_token = True
    c = make_client(KICK_BROADCASTER_USER_ID=4242)
    cb, _ = _login(c, "kick")
    assert cb.status_code == 302 and cb.headers["location"] == "../../panel?error=exchange"
    assert not any(ck.startswith("kicktts_session=") for ck in cb.headers.get_list("set-cookie"))
    assert c.get("/panel/me").status_code == 401
    assert get_value(tts_panel_logins_total, provider="kick", result="error") == before + 1


def test_login_state_cookie_cannot_be_replayed(make_client, providers):
    c = make_client(KICK_BROADCASTER_USER_ID=4242)
    r0 = c.get("/auth/kick/login", follow_redirects=False)
    state = parse_qs(urlsplit(r0.headers["location"]).query)["state"][0]
    first = c.get("/auth/kick/callback", params={"code": "c1", "state": state}, follow_redirects=False)
    assert first.headers["location"] == "../../panel"
    # the callback deleted the login cookie, so the same state cannot be used twice
    second = c.get("/auth/kick/callback", params={"code": "c2", "state": state}, follow_redirects=False)
    assert second.headers["location"] == "../../panel?error=state"
    assert len(providers.token_forms) == 1


def test_tampered_session_cookie_is_ignored(make_client):
    c = make_client(KICK_BROADCASTER_USER_ID=4242)
    _login(c, "kick")
    assert c.get("/panel/me").status_code == 200
    forged = Signer(b"not-the-server-key-not-the-server-key").sign(Identity("kick", "4242", "anildev").to_dict(), 3600)
    c.cookies.set("kicktts_session", forged)
    assert c.get("/panel/me").status_code == 401
    c.cookies.set("kicktts_session", "garbage")
    assert c.get("/panel/me").status_code == 401


def test_session_expires(make_settings, fake_engine, fake_reader, providers, monkeypatch):
    settings = make_settings(KICK_CLIENT_ID="kick-id", KICK_CLIENT_SECRET="kick-secret", KICK_BROADCASTER_USER_ID=4242, PANEL_SESSION_TTL_S=1.0)
    app = create_app(settings, engine=fake_engine, reader=fake_reader, oauth_http=providers.client())
    with TestClient(app) as c:
        _login(c, "kick")
        assert c.get("/panel/me").status_code == 200
        import time

        time.sleep(1.1)
        assert c.get("/panel/me").status_code == 401


def test_main_module_app_exposes_panel_routes():
    import app.main as m

    def walk(routes):
        for r in routes:
            if hasattr(r, "path"):
                yield r.path
            inner = getattr(r, "original_router", None)  # fastapi's _IncludedRouter wrapper
            yield from walk(getattr(inner, "routes", []) or getattr(r, "routes", []))

    paths = set(walk(m.app.routes))
    assert {"/panel", "/panel/me", "/panel/logout", "/auth/{provider}/login", "/auth/{provider}/callback"} <= paths
