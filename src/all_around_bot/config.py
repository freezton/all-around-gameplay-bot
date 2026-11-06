from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from the environment (and a local ``.env``)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    discord_token: SecretStr
    """Bot token from the Discord developer portal."""

    target_user_id: int | None = None
    """Optional: only archive this user. ``None`` archives everyone."""

    target_guild_id: int | None = None
    """Optional server restriction for the archive."""

    dev_guild_id: int | None = None
    """If set, slash commands sync to this guild only (instant, for development)."""

    database_path: Path = Path("data/bot.db")
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
