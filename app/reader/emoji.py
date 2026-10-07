"""Emoji stripping with the project's own regex (no `emoji` package).

Covers Extended_Pictographic / Emoji_Presentation ranges, regional indicators (flags), VS15/VS16,
ZWJ, skin tones, keycap sequences, tag characters and the misc-symbol / dingbat blocks.
Turkish letters and ordinary punctuation are never touched.
"""
from __future__ import annotations

import re

# Keycap sequences: digit/#/* + optional VS16 + U+20E3 are removed as a unit (the digit goes too).
_KEYCAP = r"[0-9#*]️?⃣"

_EMOJI_CHARS = (
    "\U0001F000-\U0001FAFF"  # mahjong, cards, enclosed, misc pictographs, emoticons, transport,
    #                           regional indicators U+1F1E6-1F1FF, skin tones U+1F3FB-1F3FF, supplemental
    "\U000E0020-\U000E007F"  # tag characters (subdivision flags)
    "☀-➿"  # misc symbols, dingbats
    "⌀-⏿"  # misc technical (watch, hourglass, media controls)
    "⬀-⯿"  # misc symbols and arrows (star, up arrow)
    "←-⇿"  # arrows
    "⤴⤵"  # curved arrows
    "▪▫▶◀◻-◾"  # geometric shapes used as emoji
    "™ℹⓂ"  # trade mark, information, circled M
    "〰〽㊗㊙"  # wavy dash, part alternation mark, circled ideographs
    "©®"  # copyright, registered
    "︎️"  # VS15 / VS16
    "‍"  # zero width joiner
    "⃣"  # combining enclosing keycap
)

EMOJI_RE = re.compile(f"(?:{_KEYCAP})|[{_EMOJI_CHARS}]+")
_WS = re.compile(r"\s+")


def strip_emoji(text: str) -> str:
    """Remove every emoji / pictograph sequence and collapse the resulting whitespace."""
    return _WS.sub(" ", EMOJI_RE.sub(" ", text)).strip()


def is_only_emoji(text: str) -> bool:
    """True when nothing alphanumeric remains after stripping emoji (i.e. nothing to say)."""
    return not any(ch.isalnum() for ch in strip_emoji(text))
