"""Panel login: OAuth 2.0 code grant with Kick (PKCE, `user:read`) or Discord (`identify`), signed cookies.

No session store: the session and the in-flight login state are HMAC-signed JSON cookies, so the single
pod stays stateless and nothing survives a restart except what the browser holds. Tokens from the
providers are used once, to fetch the identity, and are never stored.

Who may see the keys (`is_authorized`): the Kick account whose user id equals KICK_BROADCASTER_USER_ID
(the channel this instance reads), plus every `kick:<id|name>` / `discord:<id|name>` entry of
PANEL_ALLOWED_USERS. Everyone else can log in but gets no keys.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

KICK_AUTHORIZE_URL = "https://id.kick.com/oauth/authorize"
KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_USERS_URL = "https://api.kick.com/public/v1/users"
KICK_SCOPE = "user:read"

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/users/@me"
DISCORD_SCOPE = "identify"

PROVIDERS = ("kick", "discord")
LOGIN_STATE_TTL_S = 600.0
HTTP_TIMEOUT = httpx.Timeout(10.0)


class AuthError(Exception):
    """Readable failure shown to the user; never contains a credential."""


# -- signed cookies --------------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """`token = b64(json) . b64(hmac_sha256(key, b64(json)))`; the JSON carries its own `exp`."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 16:
            raise ValueError("signing key too short")
        self._key = key

    @classmethod
    def from_settings(cls, settings: Settings) -> "Signer":
        if settings.SESSION_SECRET is not None:
            raw = settings.SESSION_SECRET.get_secret_value().encode("utf-8")
        else:  # derived, so no new secret is needed for a working panel
            raw = hmac.new(settings.CONTROL_TOKEN.encode("utf-8"), b"kick-tts-session", hashlib.sha256).digest()
        return cls(raw)

    def sign(self, payload: dict[str, Any], ttl_s: float, now: float | None = None) -> str:
        data = dict(payload)
        data["exp"] = (time.time() if now is None else now) + ttl_s
        body = _b64e(json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return body + "." + _b64e(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())

    def unsign(self, token: str | None, now: float | None = None) -> dict[str, Any] | None:
        """The payload when the signature is valid and not expired, else None."""
        if not token or "." not in token:
            return None
        body, _, sig = token.rpartition(".")
        try:
            expected = _b64e(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(expected, sig):
                return None
            data = json.loads(_b64d(body))
        except (ValueError, UnicodeError):
            return None
        if not isinstance(data, dict):
            return None
        exp = data.get("exp")
        if not isinstance(exp, (int, float)) or (time.time() if now is None else now) >= exp:
            return None
        return data


# -- identities ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    provider: str  # "kick" | "discord"
    id: str  # provider user id, as a string (Discord snowflakes exceed 2^53)
    name: str
    avatar: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Identity | None":
        try:
            provider, id_, name = str(data["provider"]), str(data["id"]), str(data["name"])
        except (KeyError, TypeError):
            return None
        if provider not in PROVIDERS or not id_:
            return None
        avatar = data.get("avatar")
        return cls(provider, id_, name, str(avatar) if avatar else None)


def allowed_entries(settings: Settings) -> set[tuple[str, str]]:
    """`kick:anildev,discord:1234` -> {("kick", "anildev"), ("discord", "1234")}; names lower-cased."""
    out: set[tuple[str, str]] = set()
    for raw in settings.PANEL_ALLOWED_USERS.split(","):
        entry = raw.strip()
        if not entry:
            continue
        provider, sep, who = entry.partition(":")
        provider, who = provider.strip().lower(), who.strip()
        if not sep or provider not in PROVIDERS or not who:
            log.warning("PANEL_ALLOWED_USERS: ignoring entry %r (expected kick:<id|name> or discord:<id|name>)", entry)
            continue
        out.add((provider, who.lower()))
    return out


def is_authorized(settings: Settings, who: Identity) -> bool:
    if who.provider == "kick" and settings.KICK_BROADCASTER_USER_ID > 0 and who.id == str(settings.KICK_BROADCASTER_USER_ID):
        return True
    entries = allowed_entries(settings)
    return (who.provider, who.id.lower()) in entries or (who.provider, who.name.lower()) in entries


# -- providers -------------------------------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = _b64e(secrets.token_bytes(48))
    challenge = _b64e(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class Provider:
    name: str
    display: str

    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self._http = http

    def authorize_url(self, redirect_uri: str, state: str, code_challenge: str) -> str:
        raise NotImplementedError

    async def fetch_identity(self, code: str, redirect_uri: str, code_verifier: str) -> Identity:
        raise NotImplementedError

    async def _post_token(self, url: str, form: dict[str, str]) -> str:
        try:
            resp = await self._http.post(url, data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise AuthError(f"{self.display}: token request failed ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            raise AuthError(f"{self.display}: token request rejected (HTTP {resp.status_code})")
        try:
            token = resp.json().get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not token or not isinstance(token, str):
            raise AuthError(f"{self.display}: token response had no access_token")
        return token

    async def _get_json(self, url: str, token: str) -> Any:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            resp = await self._http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AuthError(f"{self.display}: profile request failed ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            raise AuthError(f"{self.display}: profile request rejected (HTTP {resp.status_code})")
        try:
            return resp.json()
        except ValueError:
            raise AuthError(f"{self.display}: profile response was not JSON") from None


class KickProvider(Provider):
    name = "kick"
    display = "Kick"

    def authorize_url(self, redirect_uri: str, state: str, code_challenge: str) -> str:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": KICK_SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return KICK_AUTHORIZE_URL + "?" + urlencode(query)

    async def fetch_identity(self, code: str, redirect_uri: str, code_verifier: str) -> Identity:
        token = await self._post_token(
            KICK_TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
        )
        body = await self._get_json(KICK_USERS_URL, token)  # no ids -> the authorised user
        users = body.get("data") if isinstance(body, dict) else None
        user = users[0] if isinstance(users, list) and users and isinstance(users[0], dict) else None
        if not user or not user.get("user_id"):
            raise AuthError("Kick: profile response had no user")
        return Identity("kick", str(user["user_id"]), str(user.get("name") or user["user_id"]), user.get("profile_picture") or None)


class DiscordProvider(Provider):
    name = "discord"
    display = "Discord"

    def authorize_url(self, redirect_uri: str, state: str, code_challenge: str) -> str:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": DISCORD_SCOPE,
            "state": state,
            "prompt": "none",
        }
        return DISCORD_AUTHORIZE_URL + "?" + urlencode(query)

    async def fetch_identity(self, code: str, redirect_uri: str, code_verifier: str) -> Identity:
        token = await self._post_token(
            DISCORD_TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        user = await self._get_json(DISCORD_ME_URL, token)
        if not isinstance(user, dict) or not user.get("id"):
            raise AuthError("Discord: profile response had no user")
        uid = str(user["id"])
        avatar_hash = user.get("avatar")
        avatar = f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png?size=128" if avatar_hash else None
        return Identity("discord", uid, str(user.get("global_name") or user.get("username") or uid), avatar)


def build_providers(settings: Settings, http: httpx.AsyncClient) -> dict[str, Provider]:
    """Only providers with both a client id and a secret are offered on the panel."""
    out: dict[str, Provider] = {}
    if settings.KICK_CLIENT_ID and settings.KICK_CLIENT_SECRET:
        out["kick"] = KickProvider(settings.KICK_CLIENT_ID, settings.KICK_CLIENT_SECRET.get_secret_value(), http)
    if settings.DISCORD_CLIENT_ID and settings.DISCORD_CLIENT_SECRET:
        out["discord"] = DiscordProvider(settings.DISCORD_CLIENT_ID, settings.DISCORD_CLIENT_SECRET.get_secret_value(), http)
    return out
