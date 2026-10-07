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
gets its own audio track.

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

Every setting is an environment variable (or a line in `.env`); see `.env.example` for the full list
with defaults. The most relevant ones: `CONTROL_TOKEN`, `OVERLAY_KEY`, `ANTHROPIC_API_KEY` (unset =
rules-only reader), `FAKE_ENGINE`, `EMA_WEIGHTS_DIR`, `TORCH_NUM_THREADS`, `MIN_KICKS`, `REWARD_TITLE`,
`COMMAND_PREFIX`, `COMMAND_ROLES`, `COMMAND_COOLDOWN_S`, `MAX_TEXT_CHARS`, `MAX_QUEUE`, `BLOCKLIST`
(empty by default; when set, matching items are dropped, never censored), `PRONOUNCE_PATH`.

## Tests

```
python -m pytest -q
```

Runs offline: no network, no API key, no model weights. The webhook tests sign with a locally generated
RSA key; the service tests use the fake engine and reader through `create_app` plus `TestClient`.

## Reader

<!-- TASK-102 (reader): rules cleaner, pronounce.yaml, AnthropicReader, compare_reader.py -->
_Section provided by the reader workstream._

## Engine and weights

<!-- TASK-103 (engine): EmaEngine, thread caps, scripts/fetch_weights.py and the weights lock -->
_Section provided by the engine workstream._

## Overlay

<!-- TASK-107 (overlay): app/static/overlay.html details, OBS setup -->
_Section provided by the overlay workstream._

## Deployment

<!-- TASK-108 (deploy): see docs/DEPLOY.md -->
_See `docs/DEPLOY.md`._

## Kick setup

<!-- TASK-109 (kick script): see docs/KICK_SETUP.md -->
_See `docs/KICK_SETUP.md`._
