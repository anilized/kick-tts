"""Repository hygiene (TASK-110): no credentials in git, ignores intact, example files hold placeholders only,
and the overlay page is really served through create_app.

Runs offline. The credential scan walks `git ls-files`; without git it falls back to walking the tree.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# Real-looking credentials. Placeholders used by tests ("sk-ant-test-placeholder", "Bearer test-token",
# "sk-ant-api03-abcdef" in the manifest scanner's self-test) are deliberately not matched.
CREDENTIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic api key": re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_-]{20,}"),
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9_.\-]{32,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),
}
PEM_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")

# Files whose content is binary or vendored documentation; never scanned.
SKIP_SUFFIXES = {".wav", ".png", ".jpg", ".ico", ".pt", ".safetensors"}
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "weights"}

# Keys in .env.example that would be secrets when filled in.
ENV_SECRET_KEYS = {
    "CONTROL_TOKEN", "OVERLAY_KEY", "ANTHROPIC_API_KEY", "KICK_CLIENT_ID", "KICK_CLIENT_SECRET", "KICK_PUBLIC_KEY_PEM",
}
PLACEHOLDER_WORDS = ("change-me", "change_me", "your-", "placeholder", "example")


def tracked_files() -> list[Path]:
    """Paths git tracks (what a `git push` would publish); falls back to a tree walk without git."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30,
        ).stdout
        files = [ROOT / p.decode("utf-8") for p in out.split(b"\0") if p]
    except (OSError, subprocess.SubprocessError):
        files = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            files.extend(Path(dirpath) / f for f in filenames)
    return [p for p in files if p.is_file() and p.suffix.lower() not in SKIP_SUFFIXES]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _generates_keys_at_runtime(path: Path, text: str) -> bool:
    """tests/ files that build RSA keys with cryptography may legitimately mention PEM markers."""
    return TESTS in path.parents and "generate_private_key" in text


# ---------------------------------------------------------------------------------------------- credentials


def test_tracked_files_contain_no_credentials():
    files = tracked_files()
    assert len(files) > 20, "tracked file list looks wrong"
    hits: list[str] = []
    for path in files:
        text = _read(path)
        rel = path.relative_to(ROOT).as_posix()
        for label, pattern in CREDENTIAL_PATTERNS.items():
            for m in pattern.finditer(text):
                hits.append(f"{rel}: {label}: {m.group(0)[:16]}...")
        if PEM_PRIVATE_KEY.search(text) and not _generates_keys_at_runtime(path, text):
            hits.append(f"{rel}: private-key PEM header")
    assert not hits, "credential-looking strings in git:\n" + "\n".join(hits)


def test_credential_patterns_are_not_vacuous():
    # built by concatenation so this file never contains a matching literal itself
    samples = {
        "anthropic api key": "sk-ant-api03-" + "A1" * 24,
        "bearer token": "Bearer " + "x9" * 20,
        "aws access key": "AKIA" + "ABCDEFGHIJKLMNOP",
        "github token": "ghp_" + "a1" * 20,
        "slack token": "xoxb-" + "1234567890-abcdefghijklmnop",
    }
    for label, sample in samples.items():
        assert CREDENTIAL_PATTERNS[label].search(sample), label
    assert PEM_PRIVATE_KEY.search("-----BEGIN " + "RSA PRIVATE KEY-----")
    assert PEM_PRIVATE_KEY.search("-----BEGIN " + "PRIVATE KEY-----")
    # placeholders the test-suite uses must stay allowed
    assert not CREDENTIAL_PATTERNS["anthropic api key"].search("sk-ant-test-placeholder")
    assert not CREDENTIAL_PATTERNS["anthropic api key"].search("sk-ant-api03-abcdef")
    assert not CREDENTIAL_PATTERNS["bearer token"].search("Authorization: Bearer test-token")
    assert not CREDENTIAL_PATTERNS["bearer token"].search("Bearer <CONTROL_TOKEN>")


def test_no_secret_or_weight_files_are_tracked():
    rel = {p.relative_to(ROOT).as_posix() for p in tracked_files()}
    offenders = [
        p for p in rel
        if p == ".env" or p.startswith("weights/") or p.endswith((".pt", ".safetensors", ".local.yaml"))
    ]
    assert not offenders, offenders


# ---------------------------------------------------------------------------------------------- .gitignore


def test_gitignore_excludes_env_and_weights():
    lines = {
        ln.strip() for ln in _read(ROOT / ".gitignore").splitlines()
        if ln.strip() and not ln.startswith("#")
    }
    assert ".env" in lines
    assert "weights/" in lines or "weights" in lines
    assert "*.pt" in lines
    assert "*.local.yaml" in lines  # deploy/secret.local.yaml, the filled-in secret


def test_gitignore_is_effective_for_secret_paths():
    try:
        subprocess.run(["git", "--version"], cwd=ROOT, capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git not available")
    for candidate in (".env", "weights/ema.pt", "weights/config.json", "model.pt", "deploy/secret.local.yaml"):
        r = subprocess.run(["git", "check-ignore", "-q", candidate], cwd=ROOT, capture_output=True, timeout=30)
        assert r.returncode == 0, f"{candidate} is not git-ignored"


# ---------------------------------------------------------------------------------------------- example files


def _parse_env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in _read(ROOT / ".env.example").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def test_env_example_holds_placeholders_only():
    values = _parse_env_example()
    assert {"CONTROL_TOKEN", "OVERLAY_KEY", "ANTHROPIC_API_KEY"} <= values.keys()
    for key in ENV_SECRET_KEYS & values.keys():
        value = values[key]
        assert value == "" or any(w in value.lower() for w in PLACEHOLDER_WORDS), f".env.example {key}={value!r}"
    for value in values.values():
        for label, pattern in CREDENTIAL_PATTERNS.items():
            assert not pattern.search(value), f".env.example contains a {label}"


def test_deploy_secret_example_holds_placeholders_only():
    doc = yaml.safe_load(_read(ROOT / "deploy" / "secret.example.yaml"))
    assert doc["kind"] == "Secret"
    data = doc.get("stringData") or {}
    assert not doc.get("data"), "base64 data block would hide real values"
    assert {"CONTROL_TOKEN", "OVERLAY_KEY", "ANTHROPIC_API_KEY", "KICK_CLIENT_ID", "KICK_CLIENT_SECRET"} <= data.keys()
    for key, value in data.items():
        value = str(value)
        ok = value.upper() == "CHANGE_ME" or value.isdigit() or any(w in value.lower() for w in PLACEHOLDER_WORDS)
        assert ok, f"secret.example.yaml {key}={value!r} is not a placeholder"


# ---------------------------------------------------------------------------------------------- overlay route


def test_overlay_html_is_served_through_create_app(settings, fake_engine, fake_reader):
    expected = (ROOT / "app" / "static" / "overlay.html").read_text(encoding="utf-8")
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert c.get("/overlay").status_code == 403
        assert c.get("/overlay", params={"key": "wrong"}).status_code == 403
        r = c.get("/overlay", params={"key": settings.OVERLAY_KEY})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert r.text == expected
        assert "TTS" in r.text and "location.href" in r.text
