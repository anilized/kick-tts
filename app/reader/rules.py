"""Rules cleaner: Kick chat text -> text EMA Lightning reads the way a Turkish streamer would say it.

Port of docs/chatclean.py. All pronunciation tables (slang, say_as, letter, foreign, vowels) live in
pronounce.yaml; this module only holds the regexes and the pipeline.

Style rules (agreed with Anil):
- Emoji are removed, never read.
- Gamer acronyms and emote names are NOT translated, only respelled: GG WP -> "gege vepe", KEKW -> "kekve".
- Keyboard smashes and laughter are read as written, only capped in length.
- Turkish chat abbreviations are expanded to the real words (see the slang table).
- Swearing is kept.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.reader.emoji import strip_emoji

log = logging.getLogger(__name__)

DEFAULT_PRONOUNCE_PATH = Path(__file__).with_name("pronounce.yaml")
DEFAULT_MAX_CHARS = 200
DEFAULT_MAX_TOKEN = 20     # a keyboard smash longer than this is cut
DEFAULT_MAX_REPEATS = 3    # "KEKW KEKW KEKW KEKW KEKW" -> three times

EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")
URL = re.compile(r"https?://\S+|www\.\S+", re.I)
MENTION = re.compile(r"@(\w+(?:[.\-]\w+)*)")
SEPARATORS = re.compile(r"[_.\-]+")
SMILEY = re.compile(r"(?<!\w)(?:[:;=8][-^']?[)(DPpOo3/\\|*]+|<3|\^\^|-_-)(?!\w)")
REPEAT_CHAR = re.compile(r"(.)\1{2,}")               # çooooook -> çook, !!!!! -> !!
REPEAT_UNIT = re.compile(r"(\w{2,3}?)\1{3,}")        # hahahahahaha -> hahaha, jsjsjsjsjs -> jsjsjs
WORD = re.compile(r"[a-zçğıöşü]+")
_NOT_WORD_CHAR = r"(?<![\wçğıöşü])(?:{keys})(?![\wçğıöşü])"


def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


@dataclass(frozen=True)
class Tables:
    slang: dict[str, str]
    say_as: dict[str, str]
    letter: dict[str, str]
    foreign: dict[str, str]
    vowels: frozenset[str]
    max_token: int
    max_repeats: int
    slang_re: re.Pattern | None


def _string_map(raw: Any, name: str, path: Path) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: table '{name}' must be a mapping")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"{path}: table '{name}' entry {key!r}: {value!r} must be string -> string "
                             "(quote the key and value)")
        out[tr_lower(key)] = value
    return out


def _parse_tables(data: Any, path: Path) -> Tables:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    slang = _string_map(data.get("slang", {}), "slang", path)
    limits = data.get("limits") or {}
    if not isinstance(limits, dict):
        raise ValueError(f"{path}: 'limits' must be a mapping")
    vowels = data.get("vowels")
    if not isinstance(vowels, str) or not vowels:
        raise ValueError(f"{path}: 'vowels' must be a non-empty string")
    slang_re = None
    if slang:
        keys = sorted(slang, key=len, reverse=True)
        slang_re = re.compile(_NOT_WORD_CHAR.format(keys="|".join(map(re.escape, keys))))
    return Tables(
        slang=slang,
        say_as=_string_map(data.get("say_as", {}), "say_as", path),
        letter=_string_map(data.get("letter", {}), "letter", path),
        foreign=_string_map(data.get("foreign", {}), "foreign", path),
        vowels=frozenset(tr_lower(vowels)),
        max_token=int(limits.get("max_token", DEFAULT_MAX_TOKEN)),
        max_repeats=int(limits.get("max_repeats", DEFAULT_MAX_REPEATS)),
        slang_re=slang_re,
    )


# Loaded once per (path, mtime, size): the same file is shared by every reader instance.
_CACHE: dict[tuple[str, int, int], Tables] = {}


def load_tables(path: Path | str) -> Tables:
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    tables = _CACHE.get(key)
    if tables is None:
        with p.open("r", encoding="utf-8") as fh:
            tables = _parse_tables(yaml.safe_load(fh), p)
        _CACHE[key] = tables
    return tables


class RulesReader:
    name = "rules"

    def __init__(self, settings_or_path: Any = None, max_chars: int | None = None):
        """Accepts a Settings object (PRONOUNCE_PATH, MAX_TEXT_CHARS), a path, or None for the packaged file."""
        path: Path | str = DEFAULT_PRONOUNCE_PATH
        chars = DEFAULT_MAX_CHARS
        if isinstance(settings_or_path, (str, Path)):
            path = settings_or_path
        elif settings_or_path is not None:
            path = getattr(settings_or_path, "PRONOUNCE_PATH", None) or path
            chars = getattr(settings_or_path, "MAX_TEXT_CHARS", None) or chars
        self.path = Path(path)
        self.max_chars = max_chars or chars
        self.tables = load_tables(self.path)

    async def read(self, text: str, user: str) -> str:
        try:
            return self.clean(text)
        except Exception:  # the protocol says read() never raises
            log.exception("rules reader failed; falling back to plain lowercase")
            try:
                return " ".join(tr_lower(strip_emoji(text)).split())[: self.max_chars]
            except Exception:
                return ""

    def say_like_chat(self, word: str) -> str:
        """Respell a token so the Turkish model reads it the way chat says it."""
        tb = self.tables
        if word in tb.say_as:
            return tb.say_as[word]
        if not any(c in tb.vowels for c in word):         # gg, wp, xd, jsjsjs -> every letter by its name
            return "".join(tb.letter.get(c, c) for c in word)
        if any(c in tb.foreign for c in word):            # kekw -> kekve, wow -> vov
            out = []
            for i, c in enumerate(word):
                if c in tb.foreign:
                    near_vowel = (i > 0 and word[i - 1] in tb.vowels) or (
                        i + 1 < len(word) and word[i + 1] in tb.vowels)
                    out.append(tb.foreign[c] if near_vowel else tb.letter.get(c, c))
                else:
                    out.append(c)
            return "".join(out)
        return word                                       # normal Turkish words are left alone

    def clean(self, text: str) -> str:
        """Sync cleaner; returns '' when nothing speakable is left."""
        tb = self.tables
        t = strip_emoji(text)                             # emoji are dropped, never read
        t = EMOTE.sub(lambda m: f" {m.group(1)} ", t)
        t = URL.sub(" link ", t)
        t = MENTION.sub(lambda m: " " + SEPARATORS.sub(" ", m.group(1)) + " ", t)
        t = SMILEY.sub(" ", t)
        t = REPEAT_UNIT.sub(lambda m: m.group(1) * tb.max_repeats, t)
        t = REPEAT_CHAR.sub(r"\1\1", t)
        t = tr_lower(t)                                   # shouting is read normally, not spelled
        if tb.slang_re is not None:
            t = tb.slang_re.sub(lambda m: tb.slang[m.group(0)], t)

        tokens: list[str] = []
        last, run = None, 0
        for tok in t.split():
            tok = WORD.sub(lambda m: self.say_like_chat(m.group(0)[: tb.max_token]), tok)
            run = run + 1 if tok == last else 1
            last = tok
            if run <= tb.max_repeats:
                tokens.append(tok)
        t = " ".join(tokens)
        t = re.sub(r"\s+([,.!?])", r"\1", t).strip(" ,")
        if len(t) > self.max_chars:
            t = t[: self.max_chars].rsplit(" ", 1)[0]
        t = strip_emoji(t)
        return t if any(ch.isalnum() for ch in t) else ""
