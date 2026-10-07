# Deploying kick-tts to k3s

kick-tts runs as one pod in namespace `streaming`, behind the existing ingress-nginx at
`https://anildev.io/tts/` (the ingress strips `/tts`, so the app itself serves `/healthz`, `/ws`, ...).

**Normal path: push to `main`.** `.github/workflows/deploy.yaml` is the same pipeline as
`anildev-home-page`: GitHub Actions builds the image, pushes it to `ghcr.io/<owner>/kick-tts:<git sha>`
(plus `:latest`), then uses `kubectl` with the `KUBECONFIG_SECRET` repo secret to apply `deploy/*.yaml`
and waits for the rollout. Sections 1 to 3 are one-time setup; section 4 is the manual fallback that does
the same thing by hand; sections 5 and 6 are checks and day-2 operations.

Manifests live in `deploy/` and are namespace-less, so always pass `-n streaming` when applying by hand.

## 0. Pinned versions
## 0. Pinned versions

| Component | Version | Where it is pinned |
|---|---|---|
| Python | 3.12 (`python:3.12-slim`) | `Dockerfile` |
| torch | 2.14.1, **CPU wheel** from `https://download.pytorch.org/whl/cpu` | `Dockerfile` `ARG TORCH_VERSION` |
| ema-lightning | 1.0.1 | `requirements.txt` |
| normalizer-tr | 0.4.0 | `requirements.txt` |
| huggingface-hub | 2.1.1 | `requirements.txt` |
| everything else (fastapi, uvicorn, pydantic, anthropic, httpx, cryptography, numpy, pyyaml, websockets, prometheus-client, ...) | `==` pins | `requirements.txt` |
| EMA weights | Hugging Face commit sha + sha256 per file | `weights.lock.json`, `--build-arg EMA_REVISION` |

`torchaudio` and `torchvision` are never installed. `tests/test_requirements_pins.py` checks that every pin in
`requirements.txt` equals the version installed in the dev venv, so the image equals the tested combination.

### Bump procedure

1. Change the dev venv (`pip install -U <pkg>`; for torch use the CPU index).
2. Run `python -m pytest -q` in that venv.
3. Update the pins: `requirements.txt` (and `ARG TORCH_VERSION` in the `Dockerfile` for torch).
4. Push to `main`: the workflow builds a new image tagged with the commit sha and rolls it out. Every
   image stays in GHCR, so the previous sha is always available for rollback (section 6).

For new weights: pick the new commit sha, regenerate the lock (section 1), commit, push.

## 1. One-time: generate the weights lock

The Dockerfile refuses to build while `weights.lock.json` holds the placeholder, and the workflow reads the
revision to build from that file (`jq -r .revision weights.lock.json`), so the lock is the single source of
truth for which weights ship. On a machine with network access (your PC is fine, the venv needs `huggingface-hub`):

```bash
python scripts/fetch_weights.py --revision <sha> --out weights --write-lock
git add weights.lock.json && git commit -m "Pin EMA weights revision <sha>"
```

`<sha>` is the full commit sha of the EMA Lightning model repository on Hugging Face that you want to pin.
This downloads `ema.pt`, `decoder.pt` and `config.json` into `weights/` (git-ignored) and records the revision plus
the sha256 of each file in `weights.lock.json`. Review the sha256 values once, then commit the file.
Every later build re-downloads that exact revision and fails if a hash differs.

## 2. One-time: GitHub Actions access to the cluster

The workflow needs one repository secret:

| Secret | Value |
|---|---|
| `KUBECONFIG_SECRET` | The full kubeconfig file that reaches the k3s API (same value as in `anildev-home-page`) |

Set it from your PC with the GitHub CLI, pointing at the kubeconfig you already use for the cluster:

```bash
gh secret set KUBECONFIG_SECRET -R anilized/kick-tts < ~/.kube/config
```

Images are pulled from GHCR with `ghcr-pull-secret`, which the workflow re-creates in `streaming` on every
deploy from the job's `GITHUB_TOKEN`. That token expires after the run, which is fine on the single-node
cluster because `imagePullPolicy: IfNotPresent` keeps using the image already on the node. If the node is
ever rebuilt, re-run the workflow (Actions > "Build, Push, and Deploy to k3s" > Run workflow) to refresh the
pull secret and re-pull the image.

The workflow also fails early, with a clear message, when the `kick-tts-secrets` Secret from section 3 does not
exist yet: it never creates or overwrites that secret.

## 3. Create the secret

```bash
cp deploy/secret.example.yaml deploy/secret.local.yaml   # *.local.yaml is git-ignored
# edit deploy/secret.local.yaml: replace every CHANGE_ME
```

| Key | Purpose |
|---|---|
| `CONTROL_TOKEN` | Bearer token for `/speak`, `/skip`, `/clear`, `/pause`, `/resume`, `/status` |
| `OVERLAY_KEY` | `?key=` for `/overlay` and `/ws` (part of the OBS browser-source URL) |
| `ANTHROPIC_API_KEY` | LLM reader. Delete the line for rules-only mode |
| `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET` | Kick developer app, used by the `kick-tts-subscribe` CronJob |
| `KICK_BROADCASTER_USER_ID` | `0` accepts any broadcaster; set the real id after `kick_subscribe.py resolve` (see `docs/KICK_SETUP.md`) |

Generate tokens with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Never commit the filled file.

## 4. Apply by hand (fallback)

Pushing to `main` does everything in this section automatically (namespace, pull secret, ConfigMap, manifests
with the image placeholder `ghcr.io/GITHUB_OWNER/kick-tts:IMAGE_TAG` replaced by the sha-tagged image,
`rollout status`, smoke curls). Use the steps below only when Actions is unavailable or you want to apply a
local change without pushing.

Build the image locally and import it into k3s (the workflow does the same build with `EMA_REVISION` read from
`weights.lock.json`, then pushes to GHCR instead of importing):

```bash
docker build --build-arg EMA_REVISION=<sha> -t kick-tts:<tag> .
docker save kick-tts:<tag> | sudo k3s ctr images import -
```

`EMA_REVISION` must be the sha in `weights.lock.json`; the build fails when it is empty or when any hash
mismatches. For a locally imported image, replace the placeholder with your tag when applying
(`sed "s|ghcr.io/GITHUB_OWNER/kick-tts:IMAGE_TAG|kick-tts:<tag>|"` on `deployment.yaml` and `cronjob.yaml`),
exactly as the workflow does with the GHCR tag.

The pronunciation table (`app/reader/pronounce.yaml`) and the tunables file (`app/settings.yaml`, e.g. `SPEECH_SPEED`)
are mounted at `/config` from one ConfigMap generated from the repo files (there is no ConfigMap YAML in git):

```bash
kubectl create configmap kick-tts-config --from-file=pronounce.yaml=app/reader/pronounce.yaml --from-file=settings.yaml=app/settings.yaml -n streaming --dry-run=client -o yaml | kubectl apply -f -
```

Apply order (namespace, PVC and ConfigMap first, because the pod mounts them):

```bash
kubectl apply -f deploy/namespace.yaml
kubectl apply -n streaming -f deploy/pvc.yaml
# ConfigMap: the `kubectl create configmap ... | kubectl apply -f -` command above
kubectl apply -n streaming -f deploy/secret.local.yaml
kubectl apply -n streaming -f deploy/deployment.yaml
kubectl apply -n streaming -f deploy/service.yaml
kubectl apply -n streaming -f deploy/ingress.yaml
kubectl apply -n streaming -f deploy/servicemonitor.yaml   # after replacing release: CHANGE_ME, see below
kubectl apply -n streaming -f deploy/cronjob.yaml
```

Do not `apply -f deploy/` as a whole: `secret.example.yaml` would overwrite your real secret with placeholders.

### ServiceMonitor selector

The Prometheus Operator only scrapes ServiceMonitors that match its `serviceMonitorSelector`. Discover the label:

```bash
kubectl get prometheus -n monitoring -o yaml | grep -A5 serviceMonitorSelector
```

For kube-prometheus-stack this is typically `release: <helm release name>`. Put that value in the
`release:` label in `deploy/servicemonitor.yaml` before applying. If `serviceMonitorNamespaceSelector` is restrictive,
make sure it allows `streaming`.

## 5. Verify

### Pod readiness: `/readyz` is 503 at first, by design

On start the server listens within about a second, but the model is loaded and warmed up in a background task.
`/healthz` is 200 immediately; `/readyz` returns **503 (`{"status":"warming"}`) for the first ~30 s** and then 200.
The Kick webhook already accepts events during that time (they are queued). The `startupProbe` allows
60 x 5 s = 5 minutes before the pod is restarted. A `{"status":"failed"}` answer means warm-up crashed
(bad weights, OOM); check the logs.

```bash
kubectl get pods -n streaming -l app=kick-tts -w
kubectl logs -n streaming deploy/kick-tts --tail=100
```

### Smoke checks

```bash
curl -i https://anildev.io/tts/healthz            # 200
curl -i https://anildev.io/tts/readyz             # 200 once warm
curl -i "https://anildev.io/tts/overlay"          # 403 (no key)
curl -i "https://anildev.io/tts/overlay?key=<OVERLAY_KEY>"   # 200, HTML
curl -s -X POST https://anildev.io/tts/speak -H "Authorization: Bearer <CONTROL_TOKEN>" \
     -H "Content-Type: application/json" -d '{"text":"merhaba test","user":"anil"}'
```

WebSocket check (the overlay does this itself; this proves the ingress upgrade works):

```bash
# either: npx wscat -c "wss://anildev.io/tts/ws?key=<OVERLAY_KEY>"
# or:
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
     -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" "https://anildev.io/tts/ws?key=<OVERLAY_KEY>"
# expect: HTTP/1.1 101 Switching Protocols
```

Then add an OBS Browser Source with `https://anildev.io/tts/overlay?key=<OVERLAY_KEY>`, tick
"Control audio via OBS" and POST `/speak` again. In a normal browser tab audio needs one click on the page first.

### Certificate

```bash
kubectl get certificate -n streaming          # anildev-tls should be READY=True (shared with the existing ingresses)
curl -vI https://anildev.io/tts/healthz 2>&1 | grep -E "subject:|issuer:|expire date|HTTP/"
```

### Prometheus / Grafana

```bash
kubectl port-forward -n monitoring svc/<prometheus-service> 9090   # then open http://localhost:9090/targets
```

The `streaming/kick-tts` target should be UP (it scrapes the Service port `http` at `/metrics`, not through the
ingress). In Grafana > Explore try `tts_queue_length`, `tts_overlay_clients`, `rate(tts_events_received_total[5m])`,
`tts_failures_total` and `tts_reader_fallback_total`. During a stream, silence in `tts_events_received_total`
is the signal that Kick stopped delivering.
`https://anildev.io/tts/metrics` is also reachable publicly; it only exposes counters (accepted, low risk).

### Memory (RSS) after the first deploy

The pod has requests 1 Gi / limits 2 Gi on a node with 8 GB and no swap. After the first successful warm-up and a
few synthesized messages, record the real numbers and tune the limits if needed:

```bash
kubectl top pod -n streaming -l app=kick-tts
kubectl exec -n streaming deploy/kick-tts -- sh -c 'grep -E "VmRSS|VmHWM" /proc/1/status'
```

`VmHWM` is the peak. If it is close to 2 Gi, raise the limit in `deploy/deployment.yaml`; if it is far below 1 Gi,
lower the request.

## 6. Day-2 operations

### Update `pronounce.yaml` or `settings.yaml`

Normal path: edit the file, commit, push. The workflow regenerates the ConfigMap and the new pod reads it.
By hand:

```bash
kubectl create configmap kick-tts-config --from-file=pronounce.yaml=app/reader/pronounce.yaml --from-file=settings.yaml=app/settings.yaml -n streaming --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/kick-tts -n streaming
```

Both files are read at startup, so the restart is required. The strategy is `Recreate`, so the old pod stops before the
new one starts: webhooks sent in that gap (about a minute) fail, and Kick retries failed deliveries.

### Roll out a new image

Push to `main`. The run's deploy job prints the pods, the ingress and the image now set on both the Deployment
and the CronJob; `gh run watch -R anilized/kick-tts` follows it from the terminal. The workflow sets the same
sha-tagged image on the Deployment and the CronJob, so the two never drift.

Manual equivalent (for a locally imported image, see section 4):

```bash
kubectl set image deployment/kick-tts kick-tts=kick-tts:<new-tag> -n streaming
kubectl rollout status deployment/kick-tts -n streaming
kubectl set image cronjob/kick-tts-subscribe ensure=kick-tts:<new-tag> -n streaming
```

Editing `deploy/cronjob.yaml` alone changes nothing in the cluster; without `kubectl apply -n streaming -f deploy/cronjob.yaml`
the 6-hourly subscription job keeps running the previous image. Check both with
`kubectl get deploy/kick-tts cronjob/kick-tts-subscribe -n streaming -o jsonpath='{..image}'`.

### Roll back

Every deployed image stays in GHCR under its commit sha, so a rollback is either `git revert` + push (preferred,
keeps the repo and the cluster in step) or a direct `kubectl rollout undo`:

```bash
kubectl rollout undo deployment/kick-tts -n streaming
# or pin an explicit sha (the previous run's image):
kubectl set image deployment/kick-tts kick-tts=ghcr.io/anilized/kick-tts:<previous-sha> -n streaming
kubectl set image cronjob/kick-tts-subscribe ensure=ghcr.io/anilized/kick-tts:<previous-sha> -n streaming
```

`rollout undo` and `set image` only touch what they name. Keep the CronJob on the same image as the Deployment.
The next push to `main` overrides any manual change.

For locally imported images: the previous tag is still in the node's containerd store
(`sudo k3s ctr images ls | grep kick-tts`); remove old tags with `sudo k3s ctr images rm docker.io/library/kick-tts:<old-tag>`
only after the new one is stable.

### Kick subscriptions

The CronJob `kick-tts-subscribe` runs `scripts/kick_subscribe.py ensure` every 6 h to re-create webhook
subscriptions Kick removed after failed deliveries. Run it on demand with
`kubectl create job -n streaming --from=cronjob/kick-tts-subscribe ensure-now` and read the output with
`kubectl logs -n streaming job/ensure-now`. See `docs/KICK_SETUP.md` for the one-time Kick app setup.
