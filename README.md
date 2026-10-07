# kick-tts

Turkish text-to-speech for the `anildev` Kick stream. Kick webhook events (Kicks gifts, a channel
reward redemption, the `!tts` chat command) are turned into speech and played in OBS through a
browser-source overlay that shows a "🔊 TTS · user: caption" label while the audio plays.

Pipeline, all in one FastAPI process:

```
Kick webhook (RSA-verified, deduped, 200 after enqueue)
  -> event -> TtsItem mapper
  -> in-memory priority queue (kicks = manual < reward < command, cooldown for commands)
  -> one worker: Reader (LLM with rules fallback) -> Engine (EMA Lightning on CPU) -> WebSocket
  -> overlay plays one item at a time and acks with {type: "played"}
```

The reader and the engine sit behind small protocols (`app/interfaces.py`); a fake sine-tone engine and
a fake reader exist for development and tests, so nothing here needs a GPU, model weights or an API key.

## Local run

Requirements: the project virtualenv with `requirements-dev.txt` installed (Python 3.11+).

```
python scripts/dev_run.py
```

`dev_run.py` sets `FAKE_ENGINE=1` (unless you set it yourself), caps `OMP_NUM_THREADS`,
`MKL_NUM_THREADS` and `TORCH_NUM_THREADS` to 2, and uses `CONTROL_TOKEN` / `OVERLAY_KEY` from the
environment or generates fresh ones for the run. It prints the overlay URL, the token, the key and
ready-to-paste `/speak` examples, then starts uvicorn on `127.0.0.1:8000` with one worker.

- `GET /healthz` answers 200 immediately.
- `GET /readyz` answers 503 `{"status": "warming"}` until the engine warm-up finished in the background,
  then 200 `{"status": "ready"}`. With the fake engine this takes well under a second; with the real
  model expect tens of seconds.

### Open the overlay

Open `http://127.0.0.1:8000/overlay?key=<OVERLAY_KEY>` in a browser or add it as an OBS browser source
(1920x1080, transparent background). In OBS enable **Control audio via OBS** on the source so the TTS
gets its own audio track. The full OBS checklist is in [OBS overlay setup](#obs-overlay-setup).

**Browser audio unlock:** a normal browser blocks autoplaying audio until the page received one user
gesture. Click once anywhere on the overlay tab after opening it; OBS browser sources do not need this.

### Speak something

PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/speak `
  -Headers @{Authorization='Bearer <CONTROL_TOKEN>'} `
  -ContentType 'application/json; charset=utf-8' `
  -Body '{"text":"selam millet","user":"anil"}'
```

curl:

```sh
curl -X POST http://127.0.0.1:8000/speak \
  -H 'Authorization: Bearer <CONTROL_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"text":"selam millet","user":"anil"}'
```

With the fake engine you hear a short 440 Hz tone and the overlay shows `🔊 TTS · anil: selam millet`.

## Endpoints

All paths are relative and nothing ever redirects, so the service works unchanged behind the
`/tts` ingress rewrite.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | none | Liveness. Always 200, never touches the engine. |
| GET | `/readyz` | none | Readiness. 200 `ready`, else 503 with `warming` or `failed` (+ exception class name). |
| GET | `/metrics` | none | Prometheus text format. |
| GET | `/overlay?key=` | overlay key | The OBS overlay page (403 without the key). |
| WS | `/ws?key=` | overlay key | Overlay WebSocket (closed with code 1008 on a bad key). |
| POST | `/webhook/kick` | Kick signature | Kick webhook receiver; 200 after enqueue, even during warm-up. |
| POST | `/speak` | bearer | `{"text": "...", "user": "manual"}` enqueues a manual item (priority of a Kicks gift). |
| POST | `/skip` | bearer | Stop the item currently playing and move on. |
| POST | `/clear` | bearer | Empty the queue and stop the current item. |
| POST | `/pause`, `/resume` | bearer | Pause/resume dequeueing (the overlay is told via `{type: "paused"}`). |
| GET | `/status` | bearer | `{queue_length, paused, clients, readiness, error, engine, reader}`. |

Bearer auth is `Authorization: Bearer <CONTROL_TOKEN>`; both the token and the overlay key are compared
in constant time. Webhook responses: `queued`, `ignored` (nothing to say / not for us), `dropped`
(`cooldown`, `queue_full`) or `duplicate` (same `Kick-Event-Message-Id` within 10 minutes).

### WebSocket messages

Server to overlay, JSON discriminated on `type`:

- `{"type": "item", "id", "user", "caption", "audio_b64_wav", "duration_s"}`: play this, then ack.
- `{"type": "skip"}`, `{"type": "clear"}`, `{"type": "paused", "value": true|false}`, `{"type": "ping"}`.

Overlay to server: `{"type": "played", "id"}` when the audio ended or was skipped (the worker waits
for it, or for `duration_s + ACK_GRACE_S`, before sending the next item), and `{"type": "pong"}`.

## Configuration

Every setting is an environment variable (or a line in `.env`), and the non-secret tunables can also be
set in **`app/settings.yaml`**, which uses the same names as keys. Precedence: environment / `.env` >
`app/settings.yaml` > built-in default. The cluster mounts that file from the `kick-tts-config` ConfigMap
(`SETTINGS_PATH=/config/settings.yaml`), regenerated by the deploy workflow on every push, so changing
`SPEECH_SPEED` is: edit the YAML, commit, push. `SETTINGS_PATH` points at another file (empty = no
file). Secrets never go in the YAML. See `.env.example` for the full list with defaults. The most
relevant ones: `CONTROL_TOKEN`, `OVERLAY_KEY`, `ANTHROPIC_API_KEY` (unset =
rules-only reader), `FAKE_ENGINE`, `EMA_WEIGHTS_DIR`, `TORCH_NUM_THREADS`, `SPEECH_SPEED`, `MIN_KICKS`, `REWARD_TITLE`,
`COMMAND_PREFIX`, `COMMAND_ROLES`, `COMMAND_COOLDOWN_S`, `MAX_TEXT_CHARS`, `MAX_QUEUE`, `BLOCKLIST`
(empty by default; when set, matching items are dropped, never censored), `PRONOUNCE_PATH`.

## Tests

```
python -m pytest -q
```

Runs offline: no network, no API key, no model weights. The webhook tests sign with a locally generated
RSA key; the service tests use the fake engine and reader through `create_app` plus `TestClient`.
`tests/test_repo_hygiene.py` scans every git-tracked file for credential-looking strings and checks
that `.env`, `weights/` and `*.pt` stay ignored. The evidence pack from the last integration run is in
`docs/VERIFICATION.md`.

## Reader

The reader turns a raw chat message into one line of speakable Turkish before it reaches the engine.
Two implementations share the `Reader` protocol (`app/interfaces.py`):

- **`RulesReader`** (`app/reader/rules.py`) is the port of `docs/chatclean.py`: emoji removed, Kick
  `[emote:ID:NAME]` tags read by their name, URLs become "link", `@user_name.tv` becomes "user name teve",
  smileys dropped, stretched letters and laughter capped (`çooooook` → `çook`, `ahahahahaha` → `ahahaha`),
  Turkish lowercasing (`I` → `ı`, `İ` → `i`), chat abbreviations expanded, and gamer acronyms respelled
  the way Turkish chat says them (`GG WP` → `gege vepe`, `KEKW` → `kekve`, `xd` → `iksde`). Swearing is
  kept as written. It is the reader whenever `ANTHROPIC_API_KEY` is unset.
- **`AnthropicReader`** (`app/reader/llm.py`) asks `READER_MODEL` (default `claude-haiku-4-5-20251001`)
  with a 1.5 s timeout (`READER_TIMEOUT_S`) and no retries. The chat message is passed as data in the user
  turn, never as instructions, so "ignore previous instructions" is simply read aloud. Whatever the model
  returns goes through code-level guards: first non-empty line only, emoji stripped, Turkish lowercase,
  repetition caps, `MAX_TEXT_CHARS` cap, and a plausibility check (rejected when empty or longer than
  `max(2 × input, input + 40)`). A timeout, API error or rejected output falls back to `RulesReader`
  and increments `tts_reader_fallback_total{reason}`. Successful answers are cached in an LRU of
  `READER_CACHE_SIZE` entries keyed on `(user, message)`.

Emoji are also stripped in code before the reader (an only-emoji message produces no item at all) and
again by the worker after it, so no emoji ever reaches the engine.

### Tuning pronunciation (`app/reader/pronounce.yaml`)

All tables live in `app/reader/pronounce.yaml`; there are no table literals in Python. Edit the file
to change how chat is read:

| Table | What it does | Example |
|---|---|---|
| `slang` | whole-word expansion of Turkish chat abbreviations; multi-word keys allowed, the longest key wins | `"slm": "selam"`, `"iyi yyn": "iyi yayınlar"` |
| `say_as` | whole-token overrides that beat the automatic letter rules | `"aq": "a kü"`, `"omg": "o em ge"` |
| `letter` | Turkish letter names for tokens that cannot form a syllable | `"g": "ge"` so `gg` → `gege` |
| `foreign` | `w`/`q`/`x` next to a vowel | `kekw` → `kekve`, `wow` → `vov` |
| `vowels`, `limits` | which letters count as vowels; `max_token` (20) and `max_repeats` (3) | |

Rules of thumb: keys are matched after Turkish lowercasing, so write them in lower case; quote every
key and value (bare `y`, `n`, `on`, `off` turn into booleans in YAML); never add replacements for swear
words. The file is read once at startup. Locally restart `dev_run.py`; in the cluster it is mounted from
the `kick-tts-config` ConfigMap, which the deploy workflow regenerates on every push (by hand: see
`docs/DEPLOY.md`, "Update pronounce.yaml or settings.yaml"). `PRONOUNCE_PATH` overrides the file location.

Check the effect offline, without weights or network:

```
python scripts/compare_reader.py tests/data/chat_samples.txt
```

It prints a table `input | rules | llm | frontend`. `llm` is `-` unless `ANTHROPIC_API_KEY` is set
(then it calls the API, one request per line). `frontend` is what the EMA text frontend would hand to the
model (`normalizer_tr` number and date expansion plus Turkish lowercasing, e.g. `100 lira` → `yüz lira`),
so you can see where the rules and the model's own normaliser disagree. Add your own lines to
`tests/data/chat_samples.txt` or pass another file; `tests/test_reader.py` pins the expected output for
the brief's table of cases.

## Engine and weights

`EmaEngine` (`app/engine/ema.py`) runs EMA Lightning 1.0.1 on CPU, built from local files only:

- It sets `OMP_NUM_THREADS` and `MKL_NUM_THREADS` to `TORCH_NUM_THREADS` (default 2) and `HF_HUB_OFFLINE=1`
  *before* torch is imported, then calls `torch.set_num_threads`. The env vars matter because
  ema_lightning infers on its own `ema-playhead` thread. Nothing in `app/main.py` imports torch; it is
  imported lazily inside the engine constructor only.
- Weights (`ema.pt`, `decoder.pt`, `config.json`) are loaded from `EMA_WEIGHTS_DIR` (default
  `/opt/weights`) through `load_acoustic`, `load_decoder`, `Frontend` and `EMA._from_parts`, so the
  runtime never calls `hf_hub_download`. Missing files raise `WeightsNotFoundError` at startup instead
  of silently falling back to the fake engine.
- `SPEECH_SPEED` (default 1.0) is passed to `say(speed=...)` on every synth. 1.0 is the model's natural
  pace; lower is slower and higher faster, within the library's 0.25 to 4 range (0.9 makes the audio
  about 13 % longer, 0.8 about 27 %). The value comes from `app/settings.yaml` (0.85), both locally and in
  the cluster; an environment variable or `.env` line overrides it. Values outside the range fail at startup.
- `EMA_BATCH_SIZE` (default 1) is assigned to the model's `_batch_size` before warm-up, which skips the
  ~21 s CPU batch-size probe. The probe cache (`XDG_CACHE_HOME`) is still mounted on a PVC in k3s.
- Warm-up synthesizes one short sentence on the shared one-thread executor as a background task after
  the server is listening; `/readyz` turns 200 when it finishes.
- `FAKE_ENGINE=1` (or an environment where `ema_lightning`/torch cannot be imported) selects
  `FakeEngine`, a 0.6 s 440 Hz sine WAV, with a loud log line.

### Weights pinning

The image never pulls "latest". `scripts/fetch_weights.py` downloads the three files from
`canberkkkkkk/ema-lightning` at one Hugging Face commit and verifies their sha256 against
`weights.lock.json` (`{"revision": "<sha>", "files": {"ema.pt": "<sha256>", ...}}`):

```
python scripts/fetch_weights.py --revision <sha> --out weights --write-lock   # one-time, needs network
python scripts/fetch_weights.py --revision <sha> --out weights                # verify (what the Dockerfile runs)
```

Exit codes: 0 ok, 1 sha256 mismatch or missing file, 2 placeholder lock without `--write-lock` or a
`--revision` that differs from the lock. The committed lock still holds the
`PLACEHOLDER_RUN_FETCH_WEIGHTS_WITH_WRITE_LOCK` marker because the lock could not be generated
offline; the Dockerfile refuses to build until the real lock is committed (step 1 of `docs/DEPLOY.md`).
`weights/` and `*.pt` are git-ignored. The Dockerfile installs `torch==2.14.1` from the PyTorch CPU
index and everything else from the `==` pins in `requirements.txt`; `tests/test_requirements_pins.py`
keeps those pins equal to the dev venv.

## Overlay

`app/static/overlay.html` is one self-contained file (no CDNs, no absolute URLs) with a transparent body
and a bottom-anchored 48 px caption that is visible only while an item plays. It builds the WebSocket URL
as `new URL('ws' + location.search, location.href)` (http → ws, https → wss), so it works both at
`http://127.0.0.1:8000/overlay?key=…` and behind the `/tts` ingress rewrite. Items are queued locally and
played one at a time through an `<audio>` element from a `data:audio/wav;base64,…` source. The caption
stays for the server-reported `duration_s` of the item even when the media element fires `ended` or
`error` early (OBS's browser engine does), and the item is force-ended 2 s after that length if `ended`
never arrives; every item is acked once with `{type: "played", id}` when it ends, on skip or on clear.
`skip` stops the current audio,
`clear` also drops the local buffer, `paused` is shown in the debug bar only, and `ping` is answered with
`pong`. The socket reconnects with 1 → 2 → 4 → 8 → 10 s backoff; on reconnect the local buffer is dropped.
Append `&debug=1` to the URL to show the connection status. A rejected `play()` (autoplay policy) shows a
"click to enable audio" hint; one click retries.

### OBS overlay setup

1. In OBS add a **Browser** source.
2. URL: `https://anildev.io/tts/overlay?key=<OVERLAY_KEY>` (add `&debug=1` to show connection status).
3. Width **1920**, height **1080**. The background is transparent, so leave the custom CSS empty.
4. Enable **Control audio via OBS**, so the TTS audio gets its own track in the Audio Mixer.
5. Turn **off** "Shutdown source when not visible". Leave "Refresh browser when scene becomes active" off too, so the WebSocket stays connected.
6. In a normal browser tab (for testing), Chrome blocks audio until the page gets a user gesture. Click once anywhere on the page, or on the "click to enable audio" hint, to unlock audio. OBS does not need this.

## Deployment

Push to `main` and `.github/workflows/deploy.yaml` does the rest, the same pipeline as `anildev-home-page`:
GitHub Actions builds the image (weights revision read from `weights.lock.json`), pushes it to
`ghcr.io/<owner>/kick-tts:<git sha>`, then applies `deploy/*.yaml` to the `streaming` namespace of the
existing k3s cluster with `kubectl` (`KUBECONFIG_SECRET` repo secret) and waits for the rollout. The
service sits behind ingress-nginx at `https://anildev.io/tts/` (path `/tts(/|$)(.*)`, rewrite to `/$2`,
TLS secret `anildev-tls`).

`deploy/` holds the namespace-less manifests: Namespace, a single-replica `Recreate` Deployment (requests
500m / 1Gi, limits 1500m / 2Gi, non-root, thread caps, `HF_HUB_OFFLINE=1`, PVC at `/cache`, startup probe on
`/readyz` with a 5 min budget, liveness on `/healthz`), Service, Ingress, PVC, ServiceMonitor, a 6-hourly
CronJob that re-ensures the Kick subscriptions, and `secret.example.yaml` as the template for
`kick-tts-secrets`. The app secret is created once by hand and never touched by CI. **`docs/DEPLOY.md`**
has the full procedure: generating the weights lock, the one-time `KUBECONFIG_SECRET` and app-secret setup,
the manual build-and-apply fallback, the ServiceMonitor selector discovery command, the note that
`/readyz` is 503 for the first ~30 s by design, certificate and Grafana checks, memory tuning,
pronounce.yaml updates, rollout and rollback.

## Kick setup

Events reach the service as webhooks from Kick's official API. One-time steps, all in
**`docs/KICK_SETUP.md`**: create the Kick developer app (2FA required), enable webhooks in the portal and
set the webhook URL to `https://anildev.io/tts/webhook/kick`, put `KICK_CLIENT_ID` / `KICK_CLIENT_SECRET`
into the secret, then subscribe the channel:

```
python scripts/kick_subscribe.py ensure     # also: token | resolve | list | delete <id>
```

`ensure` fetches an app token (`client_credentials`), resolves the `anildev` slug to a
`broadcaster_user_id`, and creates only the missing subscriptions for `chat.message.sent`,
`kicks.gifted` and `channel.reward.redemption.updated` (webhook, version 1). Kick silently
unsubscribes an app after a day of failed deliveries, which is why the webhook answers 200 right after
enqueueing and why the `kick-tts-subscribe` CronJob runs `ensure` every 6 hours. The script never prints
the secret or the token.
