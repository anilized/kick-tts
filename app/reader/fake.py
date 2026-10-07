"""FakeReader: emoji strip + whitespace collapse + Turkish lowercase. Used in dev and tests."""
from __future__ import annotations

from app.reader.emoji import is_only_emoji, strip_emoji


def _tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


class FakeReader:
    name = "fake"

    async def read(self, text: str, user: str) -> str:
        if is_only_emoji(text):
            return ""
        return " ".join(_tr_lower(strip_emoji(text)).split())
