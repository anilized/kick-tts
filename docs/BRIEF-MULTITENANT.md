# kick-tts milestone 2 — Multi-tenant: any Kick streamer logs in and gets their own TTS

## Goal

Today `kick-tts` serves exactly one channel (`anildev`): one `KICK_BROADCASTER_USER_ID`, one `OVERLAY_KEY`,
one `CONTROL_TOKEN`, one queue, one worker, one overlay hub, one panel-settings file. The Kick login on
`/panel` already works, but it only gates who may see that single set of keys.

After this milestone **any Kick streamer** opens `https://anildev.io/tts/panel`, presses "Continue with
Kick", and gets a channel of their own: their own overlay URL and keys, their own triggers, their own voice
settings, their own live controls, and Kick events of *their* channel are read aloud in *their* OBS. The
operator (Anil) approves new channels from an admin card on the same panel and sees the load.

Everything that works today for `anildev` must keep working unchanged: the existing overlay URL in OBS, the
control token in Stream Deck, the webhook subscriptions, the deploy pipeline.

Read `README.md` first: it is accurate for the current code. `docs/BRIEF.md` is milestone 1 and its
"Mission addendum" (environment, venv, test rules, offline rules) **applies to this mission unchanged**.

## Decisions already made (do not re-open)

| Topic | Decision |
|---|---|
| Tenant identity | A tenant *is* a Kick channel: primary key = Kick `broadcaster_user_id` (int). Slug and display name come from the Kick profile at login. |
| Storage | **SQLite** via the standard library `sqlite3`, one file at `TENANT_DB_PATH` (default `$XDG_CACHE_HOME/kick-tts/tenants.sqlite3`, i.e. the cache PVC in the cluster, `./.cache/...` locally). WAL mode, one connection owned by the app, all access from the event loop through a tiny repository class (sync calls are fine: every query is sub-millisecond). No ORM, no new dependency. |
| Engine | Stays **one shared EMA engine on the one-thread synth executor**. Do not add executors or threads. Each tenant has at most one synth in flight (its worker waits for its own overlay's played-ack), so the executor's FIFO already round-robins between tenants. |
| Per-tenant pipeline | Per tenant: `TtsQueue`, `OverlayHub`, `EventMapper`, `Worker` task, runtime overrides. Shared: engine, executor, polish code, Kick public-key cache, message-id dedupe, OAuth providers. |
| Routing | Webhook → tenant by `broadcaster.user_id` in the payload (unknown or not-approved tenant → `ignored`). `/overlay?key=`, `/ws?key=` → tenant by overlay key. Bearer control API → tenant by control token. Keys are 32 random bytes (urlsafe base64), looked up in a dict; compare with `hmac.compare_digest` once the candidate is found. |
| Approval | `TENANT_APPROVAL` setting: `manual` (default) or `auto`. A new tenant is `pending` until an admin approves it on the panel. Pending tenants can log in and see their status page only; their webhook events are ignored; their overlay and control token do not work (403 / 401). `MAX_TENANTS` (default 20) caps approved tenants; beyond it new logins get a "full" status. |
| Admins | The Kick account whose id equals `KICK_BROADCASTER_USER_ID` and every entry in `PANEL_ALLOWED_USERS` (unchanged syntax). Admins see every tenant and can approve, disable, delete, open any tenant's panel. Discord login stays available **for admins only** (it cannot map to a channel); a Discord login that is not in the allow list sees "not allowed". |
| Kick subscriptions | At login the scope is `user:read channel:read events:subscribe`. In the callback the **user access token** creates the three webhook subscriptions (`POST /public/v1/events/subscriptions` without `broadcaster_user_id`: Kick infers it from the user token, see `docs/kick/openapi.yaml` `PostEventsSubscriptionsParams`). The refresh token is stored **encrypted** (Fernet, `cryptography` is already a dependency; key derived from `SESSION_SECRET` or `CONTROL_TOKEN` exactly like `Signer.from_settings`). An in-app task re-runs "ensure" for every approved tenant every 6 h and on demand: first with the **app access token** + `broadcaster_user_id` (per the spec an app token may name any broadcaster), and if Kick rejects that, with the tenant's refresh token. Record the last outcome and timestamp per tenant so the panel shows subscription health. The CronJob `kick-tts-subscribe` is deleted from `deploy/`; `scripts/kick_subscribe.py` stays as the operator's manual tool and gains `--broadcaster-user-id` so it can target any tenant. |
| Reader (LLM) | One shared `AnthropicReader` built from the operator's key (`ANTHROPIC_API_KEY`, or the owner tenant's panel key for backward compatibility). A tenant may set its **own** key on the panel, which builds a private reader for that tenant. Tenants without their own key use the operator's reader subject to `READER_DAILY_CAP` calls per tenant per UTC day (default 2000); over the cap, the rules reader. Counted in `tts_reader_calls_total{tenant}`. |
| Owner tenant / migration | At startup, if `KICK_BROADCASTER_USER_ID > 0` and no tenant row exists for it, create it as `approved`, with `OVERLAY_KEY` / `CONTROL_TOKEN` from the environment as its keys and the env/YAML trigger values as its settings, and import the existing `panel-settings.json` overrides (speed, polish, key) into it. From then on the DB row is authoritative; the env keys are only the seed. The existing OBS URL and Stream Deck token therefore keep working byte-for-byte. |
| Secrets | Never in the repo. Keys and refresh tokens live in SQLite on the PVC (same trust level as the Secret in etcd, as the README already says for the panel key). Log lines never contain keys or tokens. |
| UI language | The panel gets a **Turkish/English toggle** (default from `navigator.language`, remembered in `localStorage`): every visible string lives in one `I18N` table in `panel.html`. Turkish is the audience; English stays for the operator and for screenshots in docs. |
| Metrics | Existing counters/gauges get a `tenant` label (slug). Cardinality is bounded by `MAX_TENANTS`. Add `tts_tenants{status}` gauge. |

## Data model

```sql
CREATE TABLE tenants (
  broadcaster_user_id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL,                -- kick channel slug (lower case)
  name TEXT NOT NULL,                -- display name
  avatar TEXT,
  status TEXT NOT NULL,              -- pending | approved | disabled
  overlay_key TEXT NOT NULL UNIQUE,
  control_token TEXT NOT NULL UNIQUE,
  settings_json TEXT NOT NULL,       -- TenantSettings (below), JSON
  overrides_json TEXT NOT NULL,      -- RuntimeOverrides (speed, polish, anthropic key), JSON
  kick_refresh_token_enc TEXT,       -- Fernet token or NULL
  subs_state_json TEXT,              -- {"checked_at": ..., "ok": bool, "events": {...}, "error": "..."}
  created_at REAL NOT NULL,
  approved_at REAL,
  last_login_at REAL,
  last_event_at REAL
);
CREATE TABLE schema_version (version INTEGER NOT NULL);
```

`TenantSettings` (pydantic, validated, all with the current global defaults): `min_kicks`, `reward_title`,
`reward_statuses`, `command_prefix`, `command_roles`, `command_cooldown_s`, `max_text_chars`, `max_queue`,
`max_item_age_s`, `blocklist`. Bounds: `1 <= min_kicks <= 100000`, `1 <= len(command_prefix) <= 20` and it
must start with `!`, `5 <= command_cooldown_s <= 3600`, `20 <= max_text_chars <= 500`, `1 <= max_queue <= 200`,
blocklist at most 200 entries. The global env/YAML values remain the **defaults for new tenants**.

`RuntimeOverrides` is the existing class in `app/runtime.py`; `Runtime` becomes per tenant and persists
through the repository instead of `RuntimeStore` (keep `RuntimeStore` only for the one-time import).

Per-tenant in-memory object (`TenantRuntime`): the row, `TtsQueue`, `OverlayHub`, `EventMapper`,
`Worker` task, `Runtime` (reader + overrides), a ring buffer of the **last 30 items** with their outcome
(`spoken` with the read text and duration, or `dropped` with the reason) for the panel's "Recent" list, and
the reader call counter for today. A `TenantRegistry` on `app.state` owns them: loads approved tenants at
startup, starts/stops worker tasks on approve/disable/delete, and provides `by_overlay_key`,
`by_control_token`, `by_broadcaster_id`, `all()`.

## HTTP API (changes only; everything else as in README)

| Method | Path | Auth | Change |
|---|---|---|---|
| POST | `/webhook/kick` | Kick signature | route by `broadcaster.user_id`; unknown / not approved → `{"status":"ignored","reason":"unknown_tenant"}`; `last_event_at` updated |
| GET | `/overlay?key=`, WS `/ws?key=` | overlay key | key → tenant; the page is unchanged |
| POST | `/speak`, `/skip`, `/clear`, `/pause`, `/resume`; GET `/status` | bearer | token → tenant; `/status` adds `tenant: {slug, status}` and `recent: [...]` (last 30 outcomes) |
| GET | `/panel/me` | session | `{user, role: "owner"|"admin"|"visitor", tenant: {...status, settings, keys, runtime, subs, recent...} \| null, admin: {tenants: [...summary]} \| null, providers, approval_mode}` |
| PUT | `/panel/settings` | session, approved tenant | body gains the `TenantSettings` fields; 422 with the field name on a bad value |
| POST | `/panel/keys/rotate` | session, approved tenant | `{"which": "overlay"|"control"}` → new key; the old one stops working at once; overlays reconnecting with the old key are closed with 1008 |
| POST | `/panel/subscriptions/ensure` | session, approved tenant | runs ensure for this tenant now; returns the subs state |
| POST | `/panel/tenant/delete` | session, tenant owner | deletes the row, stops the worker, best-effort deletes the Kick subscriptions; 200 then the session is logged out |
| GET | `/panel/admin/tenants` | session, admin | list with status, slug, created/approved/last event, queue length, clients, reader calls today |
| POST | `/panel/admin/tenants/{id}/{approve\|disable\|delete}` | session, admin | state changes; approve runs ensure |
| GET | `/panel/admin/tenants/{id}` | session, admin | the same payload the owner sees (admins can help a streamer) |

`/panel/me` for a logged-in Kick user whose tenant row does not exist yet **creates it** (pending or
approved depending on `TENANT_APPROVAL` and `MAX_TENANTS`), so the callback stays a plain redirect.

## Panel (app/static/panel.html) — what streamers see

Keep the rules: one self-contained file, no CDNs, relative URLs only, DOM built with `textContent`,
secrets masked until revealed, works at phone width. Keep the current look (dark, Kick green). Restructure
into these views:

1. **Login** (as today, Kick button first and big; Discord small with "operator only" text).
2. **Pending**: "Your channel `<slug>` is waiting for approval" with the status, when it was requested,
   a "Check again" button and nothing else. If `MAX_TENANTS` is reached: "The service is full right now".
   If disabled: says so.
3. **Setup checklist** (approved tenant, shown at the top until all four are green, then collapsible):
   1. Kick connected (always green after login) · 2. Webhook subscriptions: the three events with ✓/✗
   from `subs`, last checked time, "Re-subscribe" button · 3. OBS browser source: green when at least one
   overlay client is connected (`clients > 0`) · 4. First test: green when the tenant has spoken at least
   one item. Each step links to its card.
4. **OBS browser source** card: as today (URL masked/copy, debug URL, the settings list, the steps).
5. **Keys** card: overlay key, control token, base URL, plus **Rotate** buttons with a confirm dialog that
   explains what breaks (OBS source URL / Stream Deck). The "keys live in the Secret" note goes away.
6. **Channel triggers** card becomes **editable**: min Kicks (number), reward title (text), command prefix
   (text), command roles (checkboxes: broadcaster, moderator, subscriber, vip, og, everyone — "everyone" means
   no role check), cooldown (number, s), max text chars, max queue, blocklist (textarea, one word per line).
   Save / Reset-to-defaults buttons; validation errors shown next to the field.
7. **Voice & reader** card: as today (speed, polish sliders, Anthropic key with Test), plus a line showing
   "Reader: operator key · 120 / 2000 calls today" or "your own key". Remove the wording about Secret and
   settings.yaml; say "default" instead.
8. **Live** card: status tiles as today, Speak / Skip / Pause / Resume / Clear, and a new **Recent** list:
   the last 30 items, newest first, each with time, kind icon, user, the spoken text or the drop reason in
   plain words (e.g. "cooldown: user sent !tts 12 s ago", "role: viewer is not allowed", "reward title did
   not match", "blocklist"). This is how a streamer answers "why was my chat's message not read?".
9. **Stream Deck / scripts** card: as today.
10. **Danger zone**: "Delete my channel" with a typed confirmation of the slug.
11. **Admin** card (admins only, at the top): table of tenants with status chips, slug, approved/created
    dates, last event, queue, clients, reader calls today, and Approve / Disable / Delete / Open buttons.
    "Open" renders that tenant's panel in place with a banner "viewing <slug> as admin".
12. Header: avatar, name, "channel <slug>", role chip, language toggle (TR / EN), log out.

Polling: `/status` every 5 s as today (bearer); `/panel/me` on load and after every save.

## Kick API facts to rely on (vendored in `docs/kick/`)

- `POST /public/v1/events/subscriptions`: with a user token `broadcaster_user_id` is ignored and inferred
  from the token; with an app token it is required. `GET` lists the app's subscriptions (filter by
  `broadcaster_user_id`); `DELETE ?id=` deletes.
- `GET /public/v1/users` with a user token and no ids → the authorised user (`user_id`, `name`,
  `profile_picture`). `GET /public/v1/channels?broadcaster_user_id=` → `slug`.
- Token endpoint `POST https://id.kick.com/oauth/token` supports `authorization_code` (+ PKCE, as the panel
  does today), `refresh_token`, and `client_credentials`. Responses carry `refresh_token` and `expires_in`.
- The webhook URL is per app (portal); every tenant's events arrive at the same `/webhook/kick`. Every
  payload has `broadcaster.user_id`.
- Scopes list: `docs/kick/openapi.yaml`, `UserAccessToken.scopes` (`events:subscribe`, `channel:read`,
  `user:read`).

Tests must mock all of this with `httpx.MockTransport`; `tests/test_panel.py` already has a mocked Kick
and Discord to extend (add the subscriptions and refresh endpoints).

## Workstreams

| # | Workstream | Depends on | Output |
|---|---|---|---|
| 1 | **Tenant core**: `app/tenants/` (models, `TenantSettings`, repository with schema + migration + owner-tenant seed and `panel-settings.json` import, Fernet helper, `TenantRegistry`, `TenantRuntime`, recent ring buffer, reader cap), config additions (`TENANT_DB_PATH`, `TENANT_APPROVAL`, `MAX_TENANTS`, `READER_DAILY_CAP`) | — | `app/tenants/*.py`, `app/config.py`, `tests/test_tenants.py` |
| 2 | **Service routing**: webhook by broadcaster, overlay/ws by key, control API by token, per-tenant workers on the shared executor, tenant-labelled metrics, `/status` with `recent` | 1 | `app/main.py`, `app/worker.py`, `app/ws.py`, `app/metrics.py`, `tests/test_api.py`, `tests/test_webhook.py`, `tests/test_ws.py` |
| 3 | **Kick subscriptions in-app**: async client (`app/kick/subscriptions.py`) reusing the logic of `scripts/kick_subscribe.py`; subscribe at login with the user token; refresh-token storage; 6-hourly ensure task; app-token first, refresh-token fallback; `scripts/kick_subscribe.py --broadcaster-user-id`; delete `deploy/cronjob.yaml` and update `tests/test_manifests.py`, `docs/KICK_SETUP.md` | 1 | `app/kick/subscriptions.py`, `app/auth.py` (scopes, refresh token in the identity exchange), `tests/test_subscriptions.py` |
| 4 | **Panel backend**: `/panel/me` shape, tenant creation on first visit, settings PUT with `TenantSettings`, key rotation, ensure endpoint, delete, admin endpoints, role resolution | 1, 3 | `app/panel.py`, `app/auth.py`, `tests/test_panel.py` |
| 5 | **Panel frontend**: the twelve views above, I18N table, phone width | 4 (API shape agreed first, in writing, in `docs/PANEL_API.md`) | `app/static/panel.html`, `tests/test_panel_static.py` |
| 6 | **Docs + deploy**: README (multi-tenant section replacing "single-tenant" wording, new settings, migration note), `docs/DEPLOY.md` (PVC now holds the DB; backup line: `kubectl cp` of the sqlite file), `.env.example`, `deploy/secret.example.yaml`, `deploy/deployment.yaml` env, `docs/KICK_SETUP.md` (portal scopes: tick `events:subscribe`, `channel:read`, `user:read`) | 1–5 | docs, `deploy/` |

Fix the shared shapes **first** in a small task every other task depends on: `TenantSettings`,
`TenantRuntime`/`TenantRegistry` interfaces, the `/panel/me` JSON (`docs/PANEL_API.md`), and the subscription
client interface (`ensure(tenant) -> SubsState`). Then the workstreams run in parallel with disjoint files.

## Hard rules

- Everything in the milestone-1 addendum: shared venv, test command unchanged, offline tests, no network,
  fake engine/reader, no real credentials, `pathlib`, Linux target.
- **Backward compatibility test**: a test that starts the app with the milestone-1 environment
  (`KICK_BROADCASTER_USER_ID`, `OVERLAY_KEY`, `CONTROL_TOKEN`, an existing `panel-settings.json`) and checks
  that the old overlay key, the old control token and the old speed override still work, and that a webhook
  for that broadcaster is spoken.
- **Isolation tests**: two tenants; a webhook for tenant A never reaches tenant B's overlay; tenant A's
  token cannot control tenant B; a pending tenant's key is refused; rotating A's key closes A's old overlay
  socket with 1008 and leaves B connected.
- **Fairness test**: two tenants with full queues and a fake engine; the executor alternates between them
  (no tenant speaks twice before the other spoke once while both have items and connected overlays).
- Reader cap test: at the cap the tenant falls back to rules and the counter resets at the UTC day boundary
  (inject the clock).
- `tests/test_repo_hygiene.py` stays green: no credential-looking strings anywhere.
- The overlay page `app/static/overlay.html` does **not** change.
- Metrics label values are slugs, never keys or tokens.

## Definition of done

1. `pytest` passes in the integration branch with the new tests above.
2. `FAKE_ENGINE=1 python scripts/dev_run.py`, with `KICK_CLIENT_ID/SECRET` unset: the panel shows the
   login page; with them set and a mocked flow in tests: login → pending → approve (as admin) → setup
   checklist → keys → editable triggers → Speak works on that tenant's overlay.
3. The owner tenant is seeded from the old environment and the old overlay URL plays audio unchanged.
4. README, `docs/DEPLOY.md`, `docs/KICK_SETUP.md`, `docs/PANEL_API.md`, `.env.example`,
   `deploy/secret.example.yaml` updated; `deploy/cronjob.yaml` removed; manifests tests green.
5. An evidence pack appended to `docs/VERIFICATION.md` (same style as milestone 1).

## Out of scope

Multiple voices / a second engine, per-tenant pronunciation tables, billing, e-mail, a second pod, a
different database, changing the overlay page, Discord onboarding.
