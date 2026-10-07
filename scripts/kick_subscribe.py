#!/usr/bin/env python3
"""Manage the Kick webhook event subscriptions kick-tts depends on.

Subcommands: token (metadata only, never the token) | resolve [--slug S] | list | ensure | delete <id> [<id> ...]

Configuration (environment):
  KICK_CLIENT_ID, KICK_CLIENT_SECRET   Kick app credentials (required)
  KICK_BROADCASTER_USER_ID             optional; resolved from the slug when unset or 0
  KICK_CHANNEL_SLUG                    channel slug to resolve (default: anildev)

Uses an app access token (client_credentials). The webhook URL itself is configured in the
Kick developer portal, not through this API. Exit codes: 0 ok, 1 Kick/HTTP error or a
subscription that could not be created, 2 configuration/usage error. Secrets are never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

TOKEN_URL = "https://id.kick.com/oauth/token"
API_BASE = "https://api.kick.com"
SUBSCRIPTIONS_PATH = "/public/v1/events/subscriptions"
CHANNELS_PATH = "/public/v1/channels"

DEFAULT_SLUG = "anildev"
EVENT_VERSION = 1
EVENTS: tuple[str, ...] = (
    "chat.message.sent",
    "kicks.gifted",
    "channel.reward.redemption.updated",
)
TIMEOUT = httpx.Timeout(10.0)


class KickError(Exception):
    """Readable failure; the message never contains credentials."""


class KickClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(transport=transport, timeout=TIMEOUT)
        self._token: str | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KickClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- plumbing -------------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        """Redact credentials from any text that originates from a response."""
        for secret in (self._client_secret, self._token):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise KickError(f"{method} {url} failed: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            detail = self._scrub(_error_text(resp))[:200]  # scrub first so truncation cannot leave a partial secret
            raise KickError(f"{method} {url} -> HTTP {resp.status_code}: {detail}")
        return resp

    def _api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.get_token()}", "Accept": "application/json"}
        return self._send(method, API_BASE + path, headers=headers, **kwargs)

    @staticmethod
    def _data(resp: httpx.Response) -> Any:
        try:
            return resp.json().get("data")
        except (ValueError, AttributeError):
            raise KickError(f"unexpected non-JSON response from {resp.request.url.path}") from None

    # --- API ------------------------------------------------------------------------------

    def get_token(self) -> str:
        """App access token via the client_credentials grant (cached for this client)."""
        if self._token is None:
            self._token = str(self.fetch_token()["access_token"])
        return self._token

    def fetch_token(self) -> dict[str, Any]:
        resp = self._send(
            "POST",
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not body.get("access_token"):
            raise KickError("token response did not contain an access_token")
        return body

    def resolve_broadcaster_id(self, slug: str = DEFAULT_SLUG) -> int:
        resp = self._api("GET", CHANNELS_PATH, params={"slug": slug})
        channels = self._data(resp) or []
        for channel in channels:
            if str(channel.get("slug", "")).lower() == slug.lower() and channel.get("broadcaster_user_id"):
                return int(channel["broadcaster_user_id"])
        if channels and channels[0].get("broadcaster_user_id"):
            return int(channels[0]["broadcaster_user_id"])
        raise KickError(f"no channel found for slug {slug!r}")

    def list_subscriptions(self, broadcaster_user_id: int | None = None) -> list[dict[str, Any]]:
        params = {"broadcaster_user_id": broadcaster_user_id} if broadcaster_user_id else None
        resp = self._api("GET", SUBSCRIPTIONS_PATH, params=params)
        return list(self._data(resp) or [])

    def create_subscriptions(self, broadcaster_user_id: int, events: Sequence[str]) -> list[dict[str, Any]]:
        body = {
            "broadcaster_user_id": broadcaster_user_id,
            "method": "webhook",
            "events": [{"name": name, "version": EVENT_VERSION} for name in events],
        }
        resp = self._api("POST", SUBSCRIPTIONS_PATH, json=body)
        return list(self._data(resp) or [])

    def delete_subscriptions(self, ids: Sequence[str]) -> None:
        self._api("DELETE", SUBSCRIPTIONS_PATH, params=[("id", i) for i in ids])

    def ensure(self, broadcaster_user_id: int) -> dict[str, Any]:
        """Subscribe to whichever of EVENTS this broadcaster is missing (method webhook, v1)."""
        existing = self.list_subscriptions(broadcaster_user_id)
        present = {
            sub.get("event")
            for sub in existing
            if sub.get("method") == "webhook"
            and sub.get("version") == EVENT_VERSION
            and sub.get("broadcaster_user_id") == broadcaster_user_id
        }
        already = [name for name in EVENTS if name in present]
        missing = [name for name in EVENTS if name not in present]
        created: list[str] = []
        failed: dict[str, str] = {}
        if missing:
            for item in self.create_subscriptions(broadcaster_user_id, missing):
                name = self._scrub(str(item.get("name")))
                if item.get("error"):
                    failed[name] = self._scrub(str(item["error"]))[:200]
                else:
                    created.append(name)
            for name in missing:  # events the API silently omitted from its reply
                if name not in created and name not in failed:
                    failed[name] = "not confirmed in response"
        return {
            "broadcaster_user_id": broadcaster_user_id,
            "already": already,
            "created": created,
            "failed": failed,
        }


def _error_text(resp: httpx.Response) -> str:
    try:
        message = resp.json().get("message")
    except (ValueError, AttributeError):
        message = None
    return str(message or resp.reason_phrase or "error")


def _broadcaster_id(client: KickClient, env: Mapping[str, str], slug: str) -> int:
    raw = (env.get("KICK_BROADCASTER_USER_ID") or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            raise KickError("KICK_BROADCASTER_USER_ID must be an integer") from None
        if value > 0:
            return value
    return client.resolve_broadcaster_id(slug)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kick_subscribe.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("token", help="check that an app access token can be fetched; prints metadata only")
    p_resolve = sub.add_parser("resolve", help="resolve a channel slug to broadcaster_user_id")
    p_resolve.add_argument("--slug", default=None, help="channel slug (default: $KICK_CHANNEL_SLUG or anildev)")
    sub.add_parser("list", help="list event subscriptions of this app")
    sub.add_parser("ensure", help="subscribe to any of the 3 required events that are missing")
    p_delete = sub.add_parser("delete", help="delete subscriptions by id")
    p_delete.add_argument("ids", nargs="+", metavar="id")
    return parser


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    env = os.environ if env is None else env
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage; surface as a return code
        return int(exc.code or 0)

    client_id = (env.get("KICK_CLIENT_ID") or "").strip()
    client_secret = (env.get("KICK_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        print("error: KICK_CLIENT_ID and KICK_CLIENT_SECRET must be set", file=sys.stderr)
        return 2
    slug = getattr(args, "slug", None) or env.get("KICK_CHANNEL_SLUG") or DEFAULT_SLUG

    try:
        with KickClient(client_id, client_secret, transport=transport) as client:
            if args.command == "token":
                body = client.fetch_token()
                print(f"token_type={body.get('token_type', 'Bearer')} expires_in={body.get('expires_in')}")
                print("access_token=<redacted>")
            elif args.command == "resolve":
                print(f"{slug} -> broadcaster_user_id={client.resolve_broadcaster_id(slug)}")
            elif args.command == "list":
                bid = _broadcaster_id(client, env, slug)
                subs = client.list_subscriptions(bid)
                print(f"{len(subs)} subscription(s) for broadcaster_user_id={bid}")
                for s in subs:
                    print(f"  {s.get('id')}  {s.get('event')}  v{s.get('version')}  {s.get('method')}")
            elif args.command == "ensure":
                bid = _broadcaster_id(client, env, slug)
                report = client.ensure(bid)
                print(f"broadcaster_user_id={bid}")
                for name in report["already"]:
                    print(f"  ok       {name} (already subscribed)")
                for name in report["created"]:
                    print(f"  created  {name}")
                for name, err in report["failed"].items():
                    print(f"  FAILED   {name}: {err}")
                if report["failed"]:
                    return 1
            elif args.command == "delete":
                client.delete_subscriptions(args.ids)
                print(f"deleted {len(args.ids)} subscription(s)")
    except KickError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
