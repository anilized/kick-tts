"""Local dev runner: FAKE_ENGINE=1 by default, thread caps set, tokens from env or generated.

    python scripts/dev_run.py

Env (all optional): FAKE_ENGINE (default 1), CONTROL_TOKEN / OVERLAY_KEY (default: generated per run),
DEV_HOST (127.0.0.1), DEV_PORT (8000), LOG_LEVEL (info). Everything else follows app/config.py.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    # Thread caps must be in the environment before torch is first imported (only matters with FAKE_ENGINE=0).
    os.environ.setdefault("FAKE_ENGINE", "1")
    threads = os.environ.get("TORCH_NUM_THREADS", "2")
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "TORCH_NUM_THREADS"):
        os.environ.setdefault(var, threads)

    control_token = os.environ.get("CONTROL_TOKEN") or secrets.token_urlsafe(16)
    overlay_key = os.environ.get("OVERLAY_KEY") or secrets.token_urlsafe(16)
    os.environ["CONTROL_TOKEN"] = control_token
    os.environ["OVERLAY_KEY"] = overlay_key

    host = os.environ.get("DEV_HOST", "127.0.0.1")
    port = int(os.environ.get("DEV_PORT", "8000"))
    base = f"http://{host}:{port}"

    print(f"kick-tts dev server  (FAKE_ENGINE={os.environ['FAKE_ENGINE']}, threads={threads})")
    print(f"  overlay : {base}/overlay?key={overlay_key}")
    print(f"  panel   : {base}/panel   (login needs KICK_CLIENT_ID/SECRET or DISCORD_CLIENT_ID/SECRET in .env)")
    print(f"  health  : {base}/healthz   ready: {base}/readyz   metrics: {base}/metrics")
    print(f"  CONTROL_TOKEN={control_token}")
    print(f"  OVERLAY_KEY={overlay_key}")
    print()
    print("PowerShell:")
    print(
        f"  Invoke-RestMethod -Method Post -Uri {base}/speak "
        f"-Headers @{{Authorization='Bearer {control_token}'}} "
        f"-ContentType 'application/json; charset=utf-8' "
        f"-Body '{{\"text\":\"selam millet\",\"user\":\"anil\"}}'"
    )
    print("curl:")
    print(
        f"  curl -X POST {base}/speak -H 'Authorization: Bearer {control_token}' "
        f"-H 'Content-Type: application/json' -d '{{\"text\":\"selam millet\",\"user\":\"anil\"}}'"
    )
    print(f"  curl -X POST {base}/skip -H 'Authorization: Bearer {control_token}'")
    print()

    sys.path.insert(0, str(ROOT))
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        workers=1,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
