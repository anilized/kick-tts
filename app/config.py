"""Settings: environment variables, an optional .env file and a YAML tunables file.

Field names are the env var names and the YAML keys. Precedence: constructor kwargs > environment >
.env > YAML file (SETTINGS_PATH, default app/settings.yaml) > built-in defaults.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

_PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SETTINGS_PATH = _PACKAGE_DIR / "settings.yaml"


def _tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def _csv(value: str, lower=str.lower) -> list[str]:
    return [lower(part.strip()) for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # ANTHROPIC_API_KEY= / KICK_PUBLIC_KEY_PEM= in .env stay None
        case_sensitive=True,
        extra="ignore",
    )

    # YAML tunables file (see app/settings.yaml). Empty string = no file. Read in settings_customise_sources,
    # so it can only come from the environment or the constructor, never from the YAML itself.
    SETTINGS_PATH: str = str(DEFAULT_SETTINGS_PATH)

    # auth (required unless FAKE_ENGINE, then dev defaults are filled in by the validator)
    CONTROL_TOKEN: str = ""
    OVERLAY_KEY: str = ""

    # reader
    ANTHROPIC_API_KEY: SecretStr | None = None
    READER_MODEL: str = "claude-haiku-4-5-20251001"
    READER_TIMEOUT_S: float = 1.5
    READER_CACHE_SIZE: int = 512

    # engine
    FAKE_ENGINE: bool = False
    EMA_WEIGHTS_DIR: Path = Path("/opt/weights")
    EMA_BATCH_SIZE: int = 1
    TORCH_NUM_THREADS: int = 2
    SAMPLE_RATE: int = 24000
    # Speaking rate passed to EMA Lightning's say(speed=...): 1.0 = the model's natural pace, lower is
    # slower (0.85 ~ 18 % longer audio). The library accepts 0.25 .. 4.
    SPEECH_SPEED: float = 1.0

    # kick event mapping
    KICK_BROADCASTER_USER_ID: int = 0
    MIN_KICKS: int = 1
    REWARD_TITLE: str = "TTS"
    REWARD_STATUSES: str = "pending,accepted"
    COMMAND_PREFIX: str = "!tts"
    COMMAND_ROLES: str = "broadcaster,moderator,subscriber"
    COMMAND_COOLDOWN_S: float = 30

    # queue / text limits
    MAX_TEXT_CHARS: int = 200
    MAX_QUEUE: int = 50
    MAX_ITEM_AGE_S: float = 600
    BLOCKLIST: str = ""
    PRONOUNCE_PATH: Path = _PACKAGE_DIR / "reader" / "pronounce.yaml"

    # kick webhook verification
    KICK_PUBLIC_KEY_URL: str = "https://api.kick.com/public/v1/public-key"
    KICK_PUBLIC_KEY_PEM: str | None = None

    # overlay pacing
    WS_PING_INTERVAL_S: float = 20
    ACK_GRACE_S: float = 2.0

    # panel login (/panel). A provider is offered only when both its id and secret are set.
    # KICK_CLIENT_ID/SECRET are the same Kick app scripts/kick_subscribe.py uses; its redirect URL in the
    # developer portal must be <PUBLIC_BASE_URL>/auth/kick/callback.
    KICK_CLIENT_ID: str | None = None
    KICK_CLIENT_SECRET: SecretStr | None = None
    DISCORD_CLIENT_ID: str | None = None
    DISCORD_CLIENT_SECRET: SecretStr | None = None
    # Public origin (+ path prefix) the browser uses, e.g. https://anildev.io/tts. Needed for the OAuth
    # redirect URIs behind the ingress rewrite. Empty = scheme://host of the request, no prefix (local dev).
    PUBLIC_BASE_URL: str = ""
    # Signs the session/login cookies. Unset = derived from CONTROL_TOKEN (fine for one instance).
    SESSION_SECRET: SecretStr | None = None
    # Who gets the keys after logging in: kick:<user id or name>,discord:<user id or name>. The Kick account
    # with user id KICK_BROADCASTER_USER_ID (when > 0) is always allowed.
    PANEL_ALLOWED_USERS: str = ""
    PANEL_SESSION_TTL_S: float = 7 * 24 * 3600
    # Where the values set on the panel (Anthropic key, speech speed) are persisted. Must be writable and
    # survive restarts: the cache PVC in the cluster (XDG_CACHE_HOME=/cache), ./.cache locally (git-ignored).
    PANEL_STATE_PATH: Path = Path(os.environ.get("XDG_CACHE_HOME") or ".cache") / "kick-tts" / "panel-settings.json"

    LOG_LEVEL: str = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        init_kwargs = getattr(init_settings, "init_kwargs", {}) or {}
        raw = init_kwargs.get("SETTINGS_PATH", os.environ.get("SETTINGS_PATH", str(DEFAULT_SETTINGS_PATH)))
        path = str(raw or "").strip()
        if path:
            if not Path(path).is_file():
                raise ValueError(f"SETTINGS_PATH={path}: file not found")
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=path, yaml_file_encoding="utf-8"))
        sources.append(file_secret_settings)
        return tuple(sources)

    @model_validator(mode="after")
    def _require_secrets_unless_fake(self) -> "Settings":
        if not self.CONTROL_TOKEN:
            if not self.FAKE_ENGINE:
                raise ValueError("CONTROL_TOKEN is required unless FAKE_ENGINE=1")
            self.CONTROL_TOKEN = "dev-token"
        if not self.OVERLAY_KEY:
            if not self.FAKE_ENGINE:
                raise ValueError("OVERLAY_KEY is required unless FAKE_ENGINE=1")
            self.OVERLAY_KEY = "dev-key"
        if not 0.25 <= self.SPEECH_SPEED <= 4:
            raise ValueError("SPEECH_SPEED must be between 0.25 and 4")
        self.PUBLIC_BASE_URL = self.PUBLIC_BASE_URL.strip().rstrip("/")
        if self.PUBLIC_BASE_URL and not self.PUBLIC_BASE_URL.startswith(("http://", "https://")):
            raise ValueError("PUBLIC_BASE_URL must start with http:// or https://")
        return self

    # ASCII Kick API identifiers (redemption status, badge type): plain lower(), never Turkish I/ı mapping.
    @property
    def reward_statuses(self) -> list[str]:
        return _csv(self.REWARD_STATUSES)

    @property
    def command_roles(self) -> list[str]:
        return _csv(self.COMMAND_ROLES)

    # Turkish words: Turkish-aware lowercasing (İ -> i, I -> ı).
    @property
    def blocklist(self) -> list[str]:
        return _csv(self.BLOCKLIST, _tr_lower)
