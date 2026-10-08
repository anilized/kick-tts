"""AnthropicReader: Claude Haiku turns a chat message into speakable Turkish, with the rules cleaner as fallback.

The model is never trusted. Whatever it returns goes through code-level guards (emoji strip, first line,
Turkish lowercase, repetition caps, plausibility and length checks); any failure, timeout or rejected output
falls back to RulesReader.clean(). read() never raises.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import anthropic

from app.metrics import tts_reader_fallback_total, tts_reader_latency_seconds
from app.reader.emoji import strip_emoji
from app.reader.prompt import SYSTEM_PROMPT, build_user_turn
from app.reader.rules import (
    DEFAULT_MAX_REPEATS,
    DEFAULT_MAX_TOKEN,
    REPEAT_CHAR,
    REPEAT_UNIT,
    WORD,
    RulesReader,
    tr_lower,
)

log = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 200
# No temperature argument: anthropic 1.11's messages.create() no longer takes sampling parameters; determinism comes
# from the few-shot prompt, the code-level guards and the LRU instead.
_QUOTES = "\"'`“”‘’«»"


def _api_error_message(exc: Any) -> str:
    """The API's own error text ('invalid x-api-key', ...), capped and never containing the key."""
    body = getattr(exc, "body", None)
    message = None
    if isinstance(body, dict):
        err = body.get("error")
        message = err.get("message") if isinstance(err, dict) else body.get("message")
    if not message:
        message = getattr(exc, "message", None)
    return str(message or "")[:120]


def _secret(value: Any) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    raw = getter() if getter else str(value)
    return raw or None


class AnthropicReader:
    """http_client is an httpx2.AsyncClient (the Anthropic SDK 1.11 runs on httpx2, not httpx); tests inject a MockTransport."""

    name = "anthropic"

    def __init__(self, settings, fallback: RulesReader, http_client: Any = None):
        self.fallback = fallback
        self.model = settings.READER_MODEL
        self.timeout_s = float(settings.READER_TIMEOUT_S)
        self.max_chars = int(settings.MAX_TEXT_CHARS)
        self.cache_size = max(0, int(settings.READER_CACHE_SIZE))
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self.client = anthropic.AsyncAnthropic(
            api_key=_secret(settings.ANTHROPIC_API_KEY),
            timeout=self.timeout_s,
            max_retries=0,
            http_client=http_client,
        )

    async def aclose(self) -> None:
        await self.client.close()

    async def check(self) -> tuple[bool, str]:
        """One minimal request so the panel can tell a working key from a wrong one. Never raises."""
        try:
            await asyncio.wait_for(
                self.client.messages.create(model=self.model, max_tokens=1, messages=[{"role": "user", "content": "ok"}]),
                timeout=max(self.timeout_s, 5.0),
            )
        except anthropic.APIStatusError as exc:
            detail = f"HTTP {exc.status_code}: {type(exc).__name__}"
            message = _api_error_message(exc)
            if message:
                detail += f" ({message})"
            return False, detail
        except (asyncio.TimeoutError, anthropic.APITimeoutError):
            return False, "timeout"
        except Exception as exc:
            return False, f"{type(exc).__name__}"
        return True, f"ok ({self.model})"

    async def read(self, text: str, user: str) -> str:
        try:
            return await self._read(text, user)
        except Exception:  # the protocol says read() never raises
            log.exception("anthropic reader failed unexpectedly; using rules")
            return self._fallback(text, "error")

    async def _read(self, text: str, user: str) -> str:
        stripped = strip_emoji(text or "").strip()
        if not any(ch.isalnum() for ch in stripped):
            return ""                                       # nothing to say: no API call

        key = (user or "", text)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(self._call(user, stripped), timeout=self.timeout_s)
        except (asyncio.TimeoutError, anthropic.APITimeoutError):
            return self._fallback(text, "timeout")
        except Exception as exc:
            log.warning("anthropic reader call failed (%s: %s); using rules", type(exc).__name__, exc)
            return self._fallback(text, "error")
        finally:
            tts_reader_latency_seconds.labels(backend=self.name).observe(time.perf_counter() - started)

        cleaned, reason = self._guard(raw, stripped)
        if cleaned is None:
            return self._fallback(text, reason)
        if self.cache_size:
            self._cache[key] = cleaned
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return cleaned

    async def _call(self, user: str, text: str) -> str:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_turn(user, text)}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    def _guard(self, raw: str, source: str) -> tuple[str | None, str]:
        """Reduce model output to one clean speakable line; (None, reason) means reject."""
        # strip_emoji collapses newlines, so split first and strip per line
        line = next((ln for ln in map(strip_emoji, (raw or "").splitlines()) if ln), "")
        line = line.strip(_QUOTES + " ")
        line = tr_lower(" ".join(line.split()))
        if not any(ch.isalnum() for ch in line):
            return None, "empty"
        if len(line) > max(2 * len(source), len(source) + 40):   # before the caps: babbling must not be shrunk into plausibility
            return None, "too_long"
        line = self._cap_repeats(line)
        if len(line) > self.max_chars:
            line = line[: self.max_chars].rsplit(" ", 1)[0] or line[: self.max_chars]
        line = strip_emoji(line).strip()
        if not any(ch.isalnum() for ch in line):
            return None, "empty"
        return line, ""

    def _cap_repeats(self, line: str) -> str:
        tables = getattr(self.fallback, "tables", None)
        max_repeats = getattr(tables, "max_repeats", DEFAULT_MAX_REPEATS)
        max_token = getattr(tables, "max_token", DEFAULT_MAX_TOKEN)
        line = REPEAT_UNIT.sub(lambda m: m.group(1) * max_repeats, line)
        line = REPEAT_CHAR.sub(r"\1\1", line)
        out: list[str] = []
        last, run = None, 0
        for tok in line.split():
            tok = WORD.sub(lambda m: m.group(0)[:max_token], tok)
            run = run + 1 if tok == last else 1
            last = tok
            if run <= max_repeats:
                out.append(tok)
        return re.sub(r"\s+([,.!?])", r"\1", " ".join(out))

    def _fallback(self, text: str, reason: str) -> str:
        tts_reader_fallback_total.labels(reason=reason).inc()
        started = time.perf_counter()
        try:
            return self.fallback.clean(text)
        except Exception:
            log.exception("rules fallback failed")
            return ""
        finally:
            tts_reader_latency_seconds.labels(backend=self.fallback.name).observe(time.perf_counter() - started)
