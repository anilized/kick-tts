"""kick_subscribe.py against httpx.MockTransport: no network, no real credentials."""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from scripts import kick_subscribe as ks

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "s3cr3t-value-do-not-print"
ACCESS_TOKEN = "app-access-token-abc123"
BROADCASTER = 4242
ENV = {"KICK_CLIENT_ID": CLIENT_ID, "KICK_CLIENT_SECRET": CLIENT_SECRET}


def _sub(event: str, *, broadcaster: int = BROADCASTER, method: str = "webhook", version: int = 1) -> dict:
    return {
        "id": f"sub-{event}",
        "event": event,
        "broadcaster_user_id": broadcaster,
        "method": method,
        "version": version,
    }


class FakeKick:
    """Records every request and answers like the Kick API."""

    def __init__(self, existing: list[dict] | None = None, token_status: int = 200) -> None:
        self.existing = existing or []
        self.token_status = token_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, method = request.url, request.method
        if url.host == "id.kick.com" and url.path == "/oauth/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"message": "invalid client"})
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 7200})
        assert url.host == "api.kick.com", url
        assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        if url.path == "/public/v1/channels":
            return httpx.Response(200, json={"data": [{"slug": "anildev", "broadcaster_user_id": BROADCASTER}]})
        if url.path == "/public/v1/events/subscriptions":
            if method == "GET":
                return httpx.Response(200, json={"data": self.existing})
            if method == "POST":
                events = json.loads(request.content)["events"]
                return httpx.Response(
                    200,
                    json={"data": [{"name": e["name"], "version": e["version"], "subscription_id": "new"} for e in events]},
                )
            if method == "DELETE":
                return httpx.Response(204)
        return httpx.Response(404, json={"message": "not found"})

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


def _run(kick: FakeKick, *argv: str, env: dict | None = None) -> int:
    return ks.main(list(argv), env={**ENV, **(env or {})}, transport=httpx.MockTransport(kick))


def test_token_request_is_client_credentials_form_post():
    kick = FakeKick()
    with ks.KickClient(CLIENT_ID, CLIENT_SECRET, transport=httpx.MockTransport(kick)) as client:
        assert client.get_token() == ACCESS_TOKEN
    (req,) = kick.requests
    assert req.method == "POST"
    assert str(req.url) == "https://id.kick.com/oauth/token"
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = parse_qs(req.content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "client_id": [CLIENT_ID],
        "client_secret": [CLIENT_SECRET],
    }


def test_resolve_uses_channels_slug_and_returns_broadcaster_id():
    kick = FakeKick()
    with ks.KickClient(CLIENT_ID, CLIENT_SECRET, transport=httpx.MockTransport(kick)) as client:
        assert client.resolve_broadcaster_id("anildev") == BROADCASTER
    (req,) = kick.calls("GET", "/public/v1/channels")
    assert str(req.url) == "https://api.kick.com/public/v1/channels?slug=anildev"


def test_resolve_command_defaults_to_anildev(capsys):
    kick = FakeKick()
    assert _run(kick, "resolve") == 0
    assert kick.calls("GET", "/public/v1/channels")[0].url.params["slug"] == "anildev"
    assert f"broadcaster_user_id={BROADCASTER}" in capsys.readouterr().out


def test_ensure_posts_only_the_missing_event():
    kick = FakeKick(existing=[_sub("chat.message.sent"), _sub("kicks.gifted")])
    assert _run(kick, "ensure") == 0
    (post,) = kick.calls("POST", "/public/v1/events/subscriptions")
    assert post.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert json.loads(post.content) == {
        "broadcaster_user_id": BROADCASTER,
        "method": "webhook",
        "events": [{"name": "channel.reward.redemption.updated", "version": 1}],
    }


def test_ensure_posts_all_three_when_nothing_exists(capsys):
    kick = FakeKick()
    assert _run(kick, "ensure") == 0
    (post,) = kick.calls("POST", "/public/v1/events/subscriptions")
    body = json.loads(post.content)
    assert body["method"] == "webhook"
    assert body["events"] == [{"name": n, "version": 1} for n in ks.EVENTS]
    assert set(ks.EVENTS) == {"chat.message.sent", "kicks.gifted", "channel.reward.redemption.updated"}
    assert capsys.readouterr().out.count("created") == 3


def test_ensure_ignores_other_broadcasters_and_non_webhook_or_old_versions():
    kick = FakeKick(
        existing=[
            _sub("chat.message.sent", broadcaster=999),
            _sub("kicks.gifted", version=2),
            _sub("channel.reward.redemption.updated", method="websocket"),
        ]
    )
    assert _run(kick, "ensure") == 0
    (post,) = kick.calls("POST", "/public/v1/events/subscriptions")
    assert len(json.loads(post.content)["events"]) == 3


def test_ensure_posts_nothing_when_all_three_exist(capsys):
    kick = FakeKick(existing=[_sub(n) for n in ks.EVENTS])
    assert _run(kick, "ensure") == 0
    assert kick.calls("POST", "/public/v1/events/subscriptions") == []
    assert capsys.readouterr().out.count("already subscribed") == 3


def test_env_broadcaster_id_skips_channel_lookup():
    kick = FakeKick(existing=[_sub(n) for n in ks.EVENTS])
    assert _run(kick, "ensure", env={"KICK_BROADCASTER_USER_ID": str(BROADCASTER)}) == 0
    assert kick.calls("GET", "/public/v1/channels") == []


def test_zero_broadcaster_id_means_resolve_from_slug():
    kick = FakeKick(existing=[_sub(n) for n in ks.EVENTS])
    assert _run(kick, "ensure", env={"KICK_BROADCASTER_USER_ID": "0", "KICK_CHANNEL_SLUG": "other"}) == 0
    assert kick.calls("GET", "/public/v1/channels")[0].url.params["slug"] == "other"


def test_list_prints_subscriptions(capsys):
    kick = FakeKick(existing=[_sub("kicks.gifted")])
    assert _run(kick, "list") == 0
    out = capsys.readouterr().out
    assert "kicks.gifted" in out and "sub-kicks.gifted" in out


def test_delete_sends_ids_as_repeated_query_params():
    kick = FakeKick()
    assert _run(kick, "delete", "id-1", "id-2") == 0
    (req,) = kick.calls("DELETE", "/public/v1/events/subscriptions")
    assert req.url.params.get_list("id") == ["id-1", "id-2"]


def test_per_event_error_in_post_response_gives_nonzero_exit(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN})
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [{"name": "kicks.gifted", "version": 1, "error": "denied"}]})

    code = ks.main(["ensure"], env={**ENV, "KICK_BROADCASTER_USER_ID": "1"}, transport=httpx.MockTransport(handler))
    assert code == 1
    assert "denied" in capsys.readouterr().out


def test_http_401_gives_nonzero_exit_without_leaking_secrets(capsys):
    kick = FakeKick(token_status=401)
    assert _run(kick, "ensure") == 1
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "401" in captured.err
    assert CLIENT_SECRET not in text
    assert ACCESS_TOKEN not in text


def test_api_401_after_token_is_reported_without_token(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN})
        return httpx.Response(401, json={"message": f"bad token {ACCESS_TOKEN}"})

    assert ks.main(["list"], env={**ENV, "KICK_BROADCASTER_USER_ID": "1"}, transport=httpx.MockTransport(handler)) == 1
    captured = capsys.readouterr()
    assert "401" in captured.err
    assert ACCESS_TOKEN not in captured.out + captured.err


def test_network_error_is_readable_and_nonzero(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    assert ks.main(["token"], env=ENV, transport=httpx.MockTransport(handler)) == 1
    assert "ConnectError" in capsys.readouterr().err


def test_missing_credentials_is_config_error(capsys):
    assert ks.main(["list"], env={}, transport=httpx.MockTransport(FakeKick())) == 2
    assert "KICK_CLIENT_ID" in capsys.readouterr().err


def test_token_command_masks_by_default(capsys):
    assert _run(FakeKick(), "token") == 0
    out = capsys.readouterr().out
    assert ACCESS_TOKEN not in out and "expires_in=7200" in out
    assert _run(FakeKick(), "token", "--show") == 0
    assert ACCESS_TOKEN in capsys.readouterr().out


def test_unknown_slug_is_an_error(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN})
        return httpx.Response(200, json={"data": []})

    assert ks.main(["resolve", "--slug", "nobody"], env=ENV, transport=httpx.MockTransport(handler)) == 1
    assert "nobody" in capsys.readouterr().err
