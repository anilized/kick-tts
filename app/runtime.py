"""Runtime overrides set from the panel: the Anthropic API key and the speaking rate.

They are persisted as one small JSON file (PANEL_STATE_PATH, on the cache PVC in the cluster, mode 0600)
and applied live: a key change swaps the reader the worker uses, a speed change sets `engine.speed`,
which EmaEngine reads on every synth. A panel value wins over the environment / settings.yaml value;
clearing it falls back to that value again. Nothing here touches the Kubernetes Secret.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, SecretStr

from app.config import Settings
from app.interfaces import Engine, Reader

log = logging.getLogger(__name__)

SPEED_MIN, SPEED_MAX = 0.25, 4.0
UNSET: Any = object()  # "leave this field as it is" marker for Runtime.update


class RuntimeOverrides(BaseModel):
    anthropic_api_key: str | None = None  # None = use the environment value (or rules-only)
    speech_speed: float | None = None  # None = use settings.yaml / env


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
    """Never the key itself: 'sk-ant-…ab12' style, enough to recognise which key is set."""
    if not key:
        return None
    return (key[:7] + "…" if len(key) > 11 else "…") + key[-4:]


class Runtime:
    """Owns the effective reader and applies the overrides to the engine once it exists."""

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
        return self.settings.model_copy(update=update) if update else self.settings

    @property
    def env_key(self) -> str | None:
        key = self.settings.ANTHROPIC_API_KEY
        return key.get_secret_value() if key is not None and key.get_secret_value() else None

    @property
    def speech_speed(self) -> float:
        return self.overrides.speech_speed if self.overrides.speech_speed is not None else self.settings.SPEECH_SPEED

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
            "state_path": str(self.store.path),
        }

    # -- apply -------------------------------------------------------------------------------------

    def attach_engine(self, engine: Engine) -> None:
        self.engine = engine
        self._apply_speed()

    def _apply_speed(self) -> None:
        if self.engine is not None and hasattr(self.engine, "speed"):
            self.engine.speed = float(self.speech_speed)

    async def update(self, *, anthropic_api_key: Any = UNSET, speech_speed: Any = UNSET) -> dict[str, Any]:
        """Apply and persist. anthropic_api_key: str to set, "" / None to clear; speech_speed: float or None to reset.
        Raises ValueError on a bad speed. The file is written before anything is swapped."""
        new = self.overrides.model_copy()
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
        return self.describe()

    async def check_reader(self) -> dict[str, Any]:
        """One tiny request through the current reader's API client, so a wrong key shows up at once."""
        check = getattr(self.reader, "check", None)
        if check is None:
            return {"ok": True, "reader": getattr(self.reader, "name", None), "detail": "rules reader, no API involved"}
        ok, detail = await check()
        return {"ok": ok, "reader": getattr(self.reader, "name", None), "detail": detail}
