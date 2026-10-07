"""scripts/compare_reader.py runs offline against the samples file."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("normalizer_tr")

from scripts.compare_reader import main  # noqa: E402

SAMPLES = Path(__file__).parent / "data" / "chat_samples.txt"


def test_compare_reader_prints_table_and_exits_zero(capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main([str(SAMPLES)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [c.strip() for c in lines[0].split("|")] == ["input", "rules", "llm", "frontend"]
    row = next(line for line in lines if line.startswith("@anildev 100 tl"))
    cells = [c.strip() for c in row.split("|")]
    assert cells[1] == "anildev 100 lira attım teşekkürler"
    assert cells[2] == "-"                       # no API key -> no llm column
    assert "yüz lira" in cells[3]                # frontend column filled by normalizer_tr


def test_compare_reader_missing_file_is_an_error():
    assert main(["no-such-file.txt"]) == 2
