"""Compare reader stages side by side: input | rules | llm | frontend.

    python scripts/compare_reader.py [tests/data/chat_samples.txt]

- rules:    app.reader.rules.RulesReader (pronounce.yaml from PRONOUNCE_PATH or the packaged default)
- llm:      AnthropicReader output when ANTHROPIC_API_KEY is set, otherwise '-'
- frontend: what the EMA text frontend would hand to the model: normalizer_tr (numbers, dates, ...) followed
            by Turkish lowercasing, without the vocab filter. '-' only when normalizer_tr is missing.

Offline by default: no weights, no network (the llm column is the only thing that calls out, and only with a key).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SAMPLES = ROOT / "tests" / "data" / "chat_samples.txt"
HEADERS = ("input", "rules", "llm", "frontend")


def _tr_lower(s: str) -> str:
    return s.replace("İ", "i").replace("I", "ı").lower()


def _make_frontend():
    """Return a callable text -> frontend text, or None when normalizer_tr is not importable."""
    try:
        from normalizer_tr import Normalizer
    except Exception:
        return None
    try:
        from ema_lightning.frontend import POLICY
    except Exception:
        POLICY = "fallback"
    normalizer = Normalizer()

    def frontend(text: str) -> str:
        if not text:
            return ""
        spoken = normalizer.normalize(text, ambiguity_policy=POLICY).normalized_text
        return _tr_lower(spoken)

    return frontend


def _make_llm_reader():
    """Return an AnthropicReader when a key is set and the LLM reader exists, else None."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from app.config import Settings
        from app.reader import get_reader
        from app.reader.llm import AnthropicReader

        reader = get_reader(Settings(CONTROL_TOKEN="compare", OVERLAY_KEY="compare"))
        return reader if isinstance(reader, AnthropicReader) else None
    except Exception as exc:
        print(f"llm column disabled: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _render(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(r[i]) for r in [HEADERS, *rows]) for i in range(len(HEADERS))]

    def line(cells) -> str:
        return " | ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

    rule = "-+-".join("-" * w for w in widths)
    return "\n".join([line(HEADERS), rule, *(line(r) for r in rows)])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = Path(args[0]) if args else DEFAULT_SAMPLES
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    samples = [s.strip() for s in lines if s.strip()]

    from app.config import Settings
    from app.reader.rules import RulesReader

    try:
        pronounce = Settings(CONTROL_TOKEN="compare", OVERLAY_KEY="compare").PRONOUNCE_PATH
    except Exception:
        pronounce = None
    rules = RulesReader(pronounce)
    frontend = _make_frontend()
    llm = _make_llm_reader()

    async def run() -> list[tuple[str, ...]]:
        rows = []
        for text in samples:
            cleaned = rules.clean(text)
            llm_out, engine_text = "-", cleaned    # the frontend sees what the service would hand to the engine
            if llm is not None:
                engine_text = await llm.read(text, "viewer")
                llm_out = engine_text or "(empty)"
            front = "-"
            if frontend is not None:
                try:
                    front = frontend(engine_text)
                except Exception as exc:
                    front = f"(error: {type(exc).__name__})"
            rows.append((text, cleaned, llm_out, front))
        return rows

    print(_render(asyncio.run(run())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
