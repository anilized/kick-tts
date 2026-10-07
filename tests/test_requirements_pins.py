"""Every pin in requirements*.txt must equal the version installed in the shared venv."""
from __future__ import annotations

import importlib.metadata as md
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==(\S+)$")


def _lines(name: str) -> list[str]:
    text = (ROOT / name).read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


@pytest.mark.parametrize("fname", ["requirements.txt", "requirements-dev.txt"])
def test_every_line_is_pinned_and_matches_venv(fname):
    lines = _lines(fname)
    assert lines, f"{fname} is empty"
    for line in lines:
        if line.startswith("-r "):
            assert (ROOT / line[3:].strip()).is_file()
            continue
        m = PIN.match(line)
        assert m, f"{fname}: unpinned or malformed line {line!r}"
        name, _extras, version = m.groups()
        assert md.version(name) == version, f"{fname}: {name} pinned {version}, venv has {md.version(name)}"


def test_torch_not_listed():
    for fname in ("requirements.txt", "requirements-dev.txt"):
        names = {PIN.match(ln).group(1).lower() for ln in _lines(fname) if PIN.match(ln)}
        assert not names & {"torch", "torchaudio", "torchvision"}, fname


def test_required_runtime_deps_present():
    names = {PIN.match(ln).group(1).lower() for ln in _lines("requirements.txt") if PIN.match(ln)}
    assert {
        "fastapi", "uvicorn", "pydantic", "pydantic-settings", "prometheus-client", "httpx", "anthropic",
        "cryptography", "numpy", "pyyaml", "websockets", "ema-lightning", "normalizer-tr", "huggingface-hub",
    } <= names
