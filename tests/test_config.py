from __future__ import annotations

from pathlib import Path

from all_around_bot.config import Settings


def test_blank_optional_env_vars_use_defaults(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "dummy")
    monkeypatch.setenv("TARGET_USER_ID", "")
    monkeypatch.setenv("TARGET_GUILD_ID", "")
    monkeypatch.setenv("DEV_GUILD_ID", "")
    monkeypatch.setenv("DATABASE_PATH", "")

    settings = Settings(_env_file=None)

    assert settings.target_user_id is None
    assert settings.target_guild_id is None
    assert settings.dev_guild_id is None
    assert settings.database_path == Path("data/bot.db")


def test_blank_optional_dotenv_values_use_defaults(tmp_path, monkeypatch) -> None:
    for key in ("TARGET_USER_ID", "TARGET_GUILD_ID", "DEV_GUILD_ID", "DATABASE_PATH"):
        monkeypatch.delenv(key, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCORD_TOKEN=dummy\nTARGET_USER_ID=\nTARGET_GUILD_ID=\nDATABASE_PATH=\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.target_user_id is None
    assert settings.target_guild_id is None
    assert settings.database_path == Path("data/bot.db")
