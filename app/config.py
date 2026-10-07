"""Settings: environment variables plus an optional .env file. Field names are the env var names."""
from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE_DIR = Path(__file__).resolve().parent


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

    LOG_LEVEL: str = "INFO"

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
