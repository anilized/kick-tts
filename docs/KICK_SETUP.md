# Kick setup

How to connect kick-tts to your Kick channel so that gifts, channel-point rewards and `!tts`
chat commands reach the service as webhooks. You do this once; after that a CronJob keeps the
subscriptions alive.

What kick-tts subscribes to (all `method: webhook`, `version: 1`):

| Event | Used for |
| --- | --- |
| `kicks.gifted` | read the gift message aloud |
| `channel.reward.redemption.updated` | read the redeemer's text when the reward title matches `REWARD_TITLE` |
| `chat.message.sent` | `!tts <text>` commands |

Source for names and endpoints: `docs/kick/event-types.md`, `docs/kick/openapi.yaml`.

## 1. Create the Kick app

1. Enable two-factor authentication on the Kick account (required for the developer tab).
2. Open <https://kick.com/settings/developer> and create an app.
3. Kick shows a **Client ID** and a **Client Secret**. Copy both; the secret is only meant to be
   handled as a secret (see step 4).
4. The portal also asks for a redirect URL. kick-tts never runs the user OAuth flow (it uses the
   app-only `client_credentials` grant), so any valid URL you own is fine, for example
   `https://anildev.io/tts/`.

## 2. Enable webhooks and set the webhook URL (portal only)

In the same app page in the developer portal, turn webhooks on and set the webhook URL to:

```
https://anildev.io/tts/webhook/kick
```

This is **not** configurable through the API; `kick_subscribe.py` only creates the per-event
subscriptions, and Kick delivers them to whatever URL the portal holds. (The vendored docs do not
describe the portal UI itself, so look for the webhook section/toggle on the app page.)

The ingress rewrites `/tts/...` to `/...`, so the service sees `POST /webhook/kick`.

## 3. Scopes

The script authenticates as the **app** with the `client_credentials` grant (`POST
https://id.kick.com/oauth/token`). Per `docs/kick/openapi.yaml`, the endpoints it uses accept an
App Access Token with no scope list:

| Endpoint | Security in the spec |
| --- | --- |
| `GET /public/v1/channels?slug=…` | `AppAccessToken` |
| `GET` / `POST` / `DELETE /public/v1/events/subscriptions` | `AppAccessToken` |

So no scope is requested by the script. For reference, the same subscription endpoints accept a
*user* token with the `events:subscribe` scope (and channel lookup with `channel:read`). If the
portal asks you to tick scopes for the app, tick `events:subscribe` and `channel:read`. If Kick
ever rejects the redemption subscription for lack of a rewards scope, `channel:rewards:read` is
the one to add; the vendored docs do not list it as required.

## 4. Put the credentials in the secret

Real values never go into git. Create/refresh the Kubernetes secret (see
`deploy/secret.example.yaml` for the full key list) so it contains:

```
KICK_CLIENT_ID=<client id from the portal>
KICK_CLIENT_SECRET=<client secret from the portal>
```

```bash
kubectl -n streaming edit secret kick-tts-secrets      # or re-apply your private secret manifest
```

For a local run, export the same two variables in your shell (or put them in the untracked
`.env`; the script reads the process environment, so `set -a; source .env; set +a` first).

Optional variables for the script:

| Variable | Default | Meaning |
| --- | --- | --- |
| `KICK_CHANNEL_SLUG` | `anildev` | channel slug used to look up the broadcaster id |
| `KICK_BROADCASTER_USER_ID` | unset / `0` | skip the lookup and use this id |

## 5. Subscribe

The service must already be deployed and reachable at the webhook URL (see `docs/DEPLOY.md`).

```bash
# locally
export KICK_CLIENT_ID=… KICK_CLIENT_SECRET=…
python scripts/kick_subscribe.py ensure

# or inside the cluster, using the same secret the CronJob uses
kubectl -n streaming create job --from=cronjob/kick-tts-subscribe kick-tts-subscribe-manual
kubectl -n streaming logs job/kick-tts-subscribe-manual
```

`ensure` lists existing subscriptions, adds only the events that are missing for the broadcaster
(webhook, version 1), and prints a report:

```
broadcaster_user_id=123456
  ok       chat.message.sent (already subscribed)
  created  kicks.gifted
  created  channel.reward.redemption.updated
```

It is idempotent: when all three exist it makes no `POST`. Exit codes: `0` ok, `1` Kick/HTTP
error (or an event Kick refused to subscribe), `2` missing credentials. Error output never
contains the client secret or the access token.

Other subcommands:

| Command | What it does |
| --- | --- |
| `token` | check that an app token can be fetched; prints type/expiry only, the token itself is never printed |
| `resolve [--slug S]` | print the `broadcaster_user_id` for a channel slug |
| `list` | list this app's subscriptions for the broadcaster (id, event, version, method) |
| `delete <id> [<id> …]` | delete subscriptions by id (ids come from `list`) |

## 6. Auto-unsubscribe and the 6-hour CronJob

If the webhook keeps failing for more than a day (pod down, certificate problem, handler too
slow or erroring), **Kick automatically unsubscribes the app from that event**
(`docs/kick/webhook-security.md`, "Disabling of Webhooks"). Nothing tells you; the stream just
stops being read aloud.

kick-tts mitigates this two ways:

- The webhook handler answers `200` right after enqueueing (also during model warm-up) and never
  waits on the reader or the speech engine.
- The CronJob `kick-tts-subscribe` runs `python scripts/kick_subscribe.py ensure` every 6 hours,
  so a subscription Kick dropped is recreated within 6 h of the service being healthy again.
  After a long outage you can run it immediately with the `kubectl create job` command above.

## 7. Verify

1. Subscriptions exist:
   ```bash
   python scripts/kick_subscribe.py list
   ```
   You should see `chat.message.sent`, `kicks.gifted` and `channel.reward.redemption.updated`,
   all `v1` / `webhook`.
2. Events arrive: the Prometheus counter `tts_events_received_total{type=…}` increases when Kick
   delivers an event.
   ```bash
   curl -s https://anildev.io/tts/metrics | grep tts_events_received_total
   ```
   Rejected deliveries show up in `tts_webhook_rejected_total{reason=…}` (for example a bad
   signature). A flat `tts_events_received_total` during a live stream is the signal to run
   `list` / `ensure`.
3. End to end with a real event, with the overlay open in OBS (`/tts/overlay?key=…`):
   - **Chat command:** send `!tts selam` as the broadcaster, a moderator or a subscriber (the
     default `COMMAND_ROLES`); other viewers are ignored.
   - **Reward:** redeem a channel-point reward whose title is exactly `TTS` (the `REWARD_TITLE`
     default) and enter some text; it is read once on `pending`/`accepted`, not on `rejected`.
   - **Kicks gift:** gift a small amount of Kicks with a message; it is read as
     `<user> <n> kick gönderdi: <message>`. Amounts below `MIN_KICKS` are skipped.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `error: … HTTP 401` from `token` | wrong `KICK_CLIENT_ID` / `KICK_CLIENT_SECRET` |
| `error: no channel found for slug` | wrong `KICK_CHANNEL_SLUG`, or set `KICK_BROADCASTER_USER_ID` directly |
| `ensure` ok but no events arrive | webhook URL/toggle not set in the portal (step 2), or the ingress/cert is down: `curl https://anildev.io/tts/healthz` |
| events arrive but `tts_webhook_rejected_total` (signature reason) grows | public key fetch failing, or a proxy altering the request body |
| subscriptions vanish after an outage | Kick auto-unsubscribed them; run `ensure` (step 5) |
