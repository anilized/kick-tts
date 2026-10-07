#!/usr/bin/env python
"""Download the pinned EMA Lightning weights and verify them against weights.lock.json.

    python scripts/fetch_weights.py --revision <sha> --out weights/            # download + verify (build time)
    python scripts/fetch_weights.py --revision <sha> --out weights/ --write-lock   # one-time: record the hashes

Lock schema: {"revision": str, "files": {name: sha256}}. A lock still holding PLACEHOLDER is refused unless
--write-lock is given, so the image build fails until the real lock has been generated and committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO_ID = "canberkkkkkk/ema-lightning"
FILES = ("ema.pt", "decoder.pt", "config.json")
PLACEHOLDER = "PLACEHOLDER_RUN_FETCH_WEIGHTS_WITH_WRITE_LOCK"
DEFAULT_LOCK = Path(__file__).resolve().parent.parent / "weights.lock.json"
_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def load_lock(path: Path) -> dict:
    lock = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(lock, dict) or not isinstance(lock.get("revision"), str) or not isinstance(lock.get("files"), dict):
        raise ValueError(f'{path}: lock must be {{"revision": str, "files": {{name: sha256}}}}')
    return lock


def is_placeholder(lock: dict) -> bool:
    return PLACEHOLDER in lock["revision"] or any(PLACEHOLDER in str(v) for v in lock["files"].values())


def verify(directory: Path, lock: dict) -> list[str]:
    """Human-readable mismatches between the files in `directory` and the lock; [] means all good."""
    problems: list[str] = []
    files = lock["files"]
    for name in FILES:
        if name not in files:
            problems.append(f"{name}: not listed in lock")
            continue
        path = Path(directory) / name
        if not path.is_file():
            problems.append(f"{name}: missing in {directory}")
            continue
        actual = sha256_file(path)
        if actual != files[name]:
            problems.append(f"{name}: sha256 mismatch (lock {files[name]}, actual {actual})")
    return problems


def write_lock(path: Path, revision: str, directory: Path) -> dict:
    lock = {"revision": revision, "files": {name: sha256_file(Path(directory) / name) for name in FILES}}
    Path(path).write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return lock


def download(revision: str, out: Path) -> None:
    """Fetch each file at the pinned revision and copy it out of the HF cache (real files, no symlinks)."""
    from huggingface_hub import hf_hub_download

    out.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        cached = hf_hub_download(REPO_ID, name, revision=revision)
        shutil.copyfile(cached, out / name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--revision", required=True, help="full HF commit sha of the weights repo")
    ap.add_argument("--out", required=True, type=Path, help="directory to place ema.pt, decoder.pt, config.json")
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK, help="lock file (default: repo weights.lock.json)")
    ap.add_argument("--write-lock", action="store_true", help="record hashes of the downloaded files into the lock")
    args = ap.parse_args(argv)

    if not args.write_lock:
        lock = load_lock(args.lock)
        if is_placeholder(lock):
            print(f"ERROR: {args.lock} still holds the placeholder; run once with --write-lock", file=sys.stderr)
            return 2
        if lock["revision"] != args.revision:
            print(f"ERROR: --revision {args.revision} differs from lock revision {lock['revision']}", file=sys.stderr)
            return 2

    download(args.revision, args.out)

    if args.write_lock:
        lock = write_lock(args.lock, args.revision, args.out)
        print(f"wrote {args.lock}: revision {lock['revision']}")
        for name, digest in lock["files"].items():
            print(f"  {name}  {digest}")
        return 0

    problems = verify(args.out, lock)
    if problems:
        print("ERROR: weights do not match the lock:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"OK: {len(FILES)} files verified at revision {args.revision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
