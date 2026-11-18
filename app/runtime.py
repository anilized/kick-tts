"""Runtime overrides set from the panel: the Anthropic API key and the speaking rate.

They are persisted as one small JSON file (PANEL_STATE_PATH, on the cache PVC in the cluster, mode 0600)
and applied live: a key change swaps the reader the worker uses, a speed change sets `engine.speed`,
which EmaEngine reads on every synth. A panel value wins over the environment / settings.yaml value;
clearing it falls back to that value again. Nothing here touches the Kubernetes Secret.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, SecretStr

from app.config import Settings
from app.engine.eleven import ElevenLabsEngine, ElevenLabsError
from app.engine.polish import Polish
from app.engine.router import RoutedEngine
from app.interfaces import Engine, Reader

log = logging.getLogger(__name__)

SPEED_MIN, SPEED_MAX = 0.25, 4.0
UNSET: Any = object()  # "leave this field as it is" marker for Runtime.update


class RuntimeOverrides(BaseModel):
    anthropic_api_key: str | None = None  # None = use the environment value (or rules-only)
    speech_speed: float | None = None  # None = use settings.yaml / env
    deess: float | None = None  # output polish (app/engine/polish.py); None = settings.yaml / env
    treble_db: float | None = None
    target_rms_db: float | None = None
    elevenlabs_api_key: str | None = None  # None = use the environment value (or no ElevenLabs)
    elevenlabs_voice_id: str | None = None  # None = settings.yaml / env
    use_elevenlabs: bool | None = None  # None = on whenever a key exists; False = force the local EMA voice


# name -> (settings field, min, max). TARGET_RMS_DB additionally allows 0 = off.
POLISH_FIELDS: dict[str, tuple[str, float, float]] = {
    "deess": ("DEESS", 0.0, 3.0),
    "treble_db": ("TREBLE_DB", -12.0, 6.0),
    "target_rms_db": ("TARGET_RMS_DB", -40.0, -6.0),
}


class RuntimeStore:
    """JSON file persistence; a missing or unreadable file is an empty override set."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> RuntimeOverrides:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return RuntimeOverrides()
        except OSError as exc:
            log.warning("panel state %s unreadable (%s); starting without overrides", self.path, type(exc).__name__)
            return RuntimeOverrides()
        try:
            return RuntimeOverrides.model_validate(json.loads(raw))
        except (ValueError, TypeError) as exc:
            log.warning("panel state %s is corrupt (%s); starting without overrides", self.path, type(exc).__name__)
            return RuntimeOverrides()

    def save(self, overrides: RuntimeOverrides) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = overrides.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".panel-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _key_hint(key: str | None) -> str | None:
    """Never the key itself: 'sk-ant-api03-…ab12' style. The prefix shows the key *type* (Console API keys
    are sk-ant-api03-, a Claude Code OAuth token is sk-ant-oat01-, which the API rejects with 401)."""
    if not key:
        return None
    prefix_len = 13 if key.startswith("sk-ant-") else 4
    return (key[:prefix_len] + "…" if len(key) > prefix_len + 6 else "…") + key[-4:]


class Runtime:
    """Owns the effective reader and the voice routing, and applies the overrides to the engine once it exists."""

    def __init__(
        self,
        settings: Settings,
        store: RuntimeStore,
        reader_factory: Callable[[Settings], Reader],
        initial_reader: Reader | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.overrides = store.load()
        self._reader_factory = reader_factory
        self.reader: Reader = initial_reader if initial_reader is not None else reader_factory(self.effective())
        self.engine: Engine | None = None
        self.routed: RoutedEngine | None = None  # what the worker synthesizes with (local engine + ElevenLabs)
        self._remote: ElevenLabsEngine | None = None
        self._remote_sig: tuple | None = None
        if self.overrides.anthropic_api_key or self.overrides.speech_speed is not None:
            log.info(
                "panel overrides loaded from %s: key=%s speed=%s",
                store.path, "set" if self.overrides.anthropic_api_key else "-", self.overrides.speech_speed,
            )

    # -- effective values -------------------------------------------------------------------------

    def effective(self) -> Settings:
        update: dict[str, Any] = {}
        if self.overrides.anthropic_api_key:
            update["ANTHROPIC_API_KEY"] = SecretStr(self.overrides.anthropic_api_key)
        if self.overrides.speech_speed is not None:
            update["SPEECH_SPEED"] = self.overrides.speech_speed
        if self.overrides.elevenlabs_api_key:
            update["ELEVENLABS_API_KEY"] = SecretStr(self.overrides.elevenlabs_api_key)
        if self.overrides.elevenlabs_voice_id:
            update["ELEVENLABS_VOICE_ID"] = self.overrides.elevenlabs_voice_id
        return self.settings.model_copy(update=update) if update else self.settings

    @property
    def env_key(self) -> str | None:
        key = self.settings.ANTHROPIC_API_KEY
        return key.get_secret_value() if key is not None and key.get_secret_value() else None

    @property
    def speech_speed(self) -> float:
        return self.overrides.speech_speed if self.overrides.speech_speed is not None else self.settings.SPEECH_SPEED

    @property
    def eleven_key(self) -> str | None:
        if self.overrides.elevenlabs_api_key:
            return self.overrides.elevenlabs_api_key
        key = self.settings.ELEVENLABS_API_KEY
        return key.get_secret_value() if key is not None and key.get_secret_value() else None

    @property
    def eleven_voice_id(self) -> str:
        return self.overrides.elevenlabs_voice_id or self.settings.ELEVENLABS_VOICE_ID

    @property
    def eleven_active(self) -> bool:
        return bool(self.eleven_key) and bool(self.eleven_voice_id) and self.overrides.use_elevenlabs is not False

    def remote_engine(self) -> ElevenLabsEngine | None:
        """The ElevenLabs engine for the current key / voice / settings, or None (EMA only). Cached, rebuilt on change."""
        if not self.eleven_active:
            return None
        s = self.settings
        sig = (self.eleven_key, self.eleven_voice_id, s.ELEVENLABS_MODEL, s.ELEVENLABS_TIMEOUT_S, s.ELEVENLABS_STABILITY,
               s.ELEVENLABS_SIMILARITY, s.ELEVENLABS_STYLE, s.ELEVENLABS_SPEED, s.ELEVENLABS_LANGUAGE)
        if self._remote is None or sig != self._remote_sig:
            old = self._remote
            self._remote = ElevenLabsEngine(
                self.eleven_key, self.eleven_voice_id, s.ELEVENLABS_MODEL, s.ELEVENLABS_TIMEOUT_S, s.ELEVENLABS_STABILITY,
                s.ELEVENLABS_SIMILARITY, s.ELEVENLABS_STYLE, s.ELEVENLABS_SPEED, s.ELEVENLABS_LANGUAGE,
            )
            self._remote_sig = sig
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
        return self._remote

    def _polish_value(self, name: str) -> float:
        override = getattr(self.overrides, name)
        return override if override is not None else float(getattr(self.settings, POLISH_FIELDS[name][0]))

    @property
    def polish(self) -> Polish:
        """What the worker applies to every utterance (read per item, so panel changes apply at once)."""
        return Polish(
            deess=self._polish_value("deess"),
            treble_db=self._polish_value("treble_db"),
            target_rms_db=self._polish_value("target_rms_db"),
        )

    def describe(self) -> dict[str, Any]:
        """What the panel shows. The key itself never leaves the server."""
        if self.overrides.anthropic_api_key:
            key_source, hint = "panel", _key_hint(self.overrides.anthropic_api_key)
        elif self.env_key:
            key_source, hint = "env", _key_hint(self.env_key)
        else:
            key_source, hint = None, None
        return {
            "anthropic_api_key": {"configured": key_source is not None, "source": key_source, "hint": hint},
            "reader": getattr(self.reader, "name", None),
            "reader_model": self.settings.READER_MODEL,
            "speech_speed": {
                "value": self.speech_speed,
                "source": "panel" if self.overrides.speech_speed is not None else "settings",
                "default": self.settings.SPEECH_SPEED,
                "min": SPEED_MIN,
                "max": SPEED_MAX,
            },
            "engine_speed_applied": self.engine is not None and hasattr(self.engine, "speed"),
            "polish": {
                name: {
                    "value": self._polish_value(name),
                    "source": "panel" if getattr(self.overrides, name) is not None else "settings",
                    "default": float(getattr(self.settings, field)),
                    "min": lo,
                    "max": hi,
                }
                for name, (field, lo, hi) in POLISH_FIELDS.items()
            },
            "elevenlabs": {
                "configured": bool(self.eleven_key),
                "source": "panel" if self.overrides.elevenlabs_api_key else ("env" if self.eleven_key else None),
                "hint": _key_hint(self.eleven_key),
                "enabled": self.overrides.use_elevenlabs is not False,
                "active": self.eleven_active,
                "voice_id": self.eleven_voice_id,
                "voice_source": "panel" if self.overrides.elevenlabs_voice_id else "settings",
                "model": self.settings.ELEVENLABS_MODEL,
                "last_used": getattr(self.routed, "last_used", None),
                "last_error": getattr(self.routed, "last_error", None),
            },
            "state_path": str(self.store.path),
        }

    # -- apply -------------------------------------------------------------------------------------

    def attach_engine(self, engine: Engine) -> None:
        self.engine = engine
        self.routed = RoutedEngine(engine, self.remote_engine)
        self._apply_speed()

    def _apply_speed(self) -> None:
        if self.engine is not None and hasattr(self.engine, "speed"):
            self.engine.speed = float(self.speech_speed)

    async def update(
        self,
        *,
        anthropic_api_key: Any = UNSET,
        speech_speed: Any = UNSET,
        deess: Any = UNSET,
        treble_db: Any = UNSET,
        target_rms_db: Any = UNSET,
        elevenlabs_api_key: Any = UNSET,
        elevenlabs_voice_id: Any = UNSET,
        use_elevenlabs: Any = UNSET,
    ) -> dict[str, Any]:
        """Apply and persist. anthropic_api_key: str to set, "" / None to clear; the numeric fields: a number to
        set, None to reset to settings.yaml / env. Raises ValueError on a bad value. The file is written before
        anything is swapped."""
        new = self.overrides.model_copy()
        for name, incoming in (("deess", deess), ("treble_db", treble_db), ("target_rms_db", target_rms_db)):
            if incoming is UNSET:
                continue
            if incoming is None:
                setattr(new, name, None)
                continue
            try:
                value = float(incoming)
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be a number") from None
            _, lo, hi = POLISH_FIELDS[name]
            if name == "target_rms_db" and value == 0:
                pass  # 0 = normalization off
            elif not lo <= value <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi}" + (" (or 0 = off)" if name == "target_rms_db" else ""))
            setattr(new, name, round(value, 2))
        if anthropic_api_key is not UNSET:
            key = (anthropic_api_key or "").strip()
            new.anthropic_api_key = key or None
        if speech_speed is not UNSET:
            if speech_speed is None:
                new.speech_speed = None
            else:
                try:
                    value = float(speech_speed)
                except (TypeError, ValueError):
                    raise ValueError("speech_speed must be a number") from None
                if not SPEED_MIN <= value <= SPEED_MAX:
                    raise ValueError(f"speech_speed must be between {SPEED_MIN} and {SPEED_MAX}")
                new.speech_speed = round(value, 3)

        if elevenlabs_api_key is not UNSET:
            new.elevenlabs_api_key = (elevenlabs_api_key or "").strip() or None
        if elevenlabs_voice_id is not UNSET:
            vid = (elevenlabs_voice_id or "").strip()
            if vid and not vid.isalnum():
                raise ValueError("elevenlabs_voice_id must be letters and digits only")
            new.elevenlabs_voice_id = vid or None
        if use_elevenlabs is not UNSET:
            new.use_elevenlabs = None if use_elevenlabs is None else bool(use_elevenlabs)

        key_changed = new.anthropic_api_key != self.overrides.anthropic_api_key
        self.store.save(new)
        self.overrides = new
        if key_changed:
            old = self.reader
            self.reader = self._reader_factory(self.effective())
            log.info("panel: reader is now %s", getattr(self.reader, "name", "?"))
            closer = getattr(old, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:  # best effort; the old client is garbage anyway
                    log.debug("closing the previous reader failed", exc_info=True)
        self._apply_speed()
        if self.routed is not None:
            self.routed.reset_backoff()  # a new key / voice / switch gets a fresh try at once
        return self.describe()

    async def check_reader(self) -> dict[str, Any]:
        """One tiny request through the current reader's API client, so a wrong key shows up at once."""
        check = getattr(self.reader, "check", None)
        if check is None:
            return {"ok": True, "reader": getattr(self.reader, "name", None), "detail": "rules reader, no API involved"}
        ok, detail = await check()
        return {"ok": ok, "reader": getattr(self.reader, "name", None), "detail": detail}

    async def check_voice(self) -> dict[str, Any]:
        """One short real synthesis through ElevenLabs, so a wrong key, voice or empty quota shows up at once."""
        remote = self.remote_engine()
        if remote is None:
            why = "no ElevenLabs key" if not self.eleven_key else "ElevenLabs is switched off (local EMA voice)"
            return {"ok": False, "detail": why}
        try:
            wav = await asyncio.get_running_loop().run_in_executor(None, remote.synth, "Merhaba.", 24000)
        except ElevenLabsError as exc:
            return {"ok": False, "detail": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}
        if self.routed is not None:
            self.routed.reset_backoff()
        return {"ok": True, "detail": f"voice {remote.voice_id}, model {remote.model_id}, {len(wav)} bytes of audio"}
