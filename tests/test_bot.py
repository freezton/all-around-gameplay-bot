from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord

from all_around_bot import bot as bot_module
from all_around_bot.bot import AllAroundBot
from all_around_bot.config import Settings


def _bot(tmp_path: Path) -> AllAroundBot:
    return AllAroundBot(
        Settings(
            discord_token="dummy",
            dev_guild_id=1,
            database_path=tmp_path / "bot.db",
        )
    )


async def test_sync_timeout_does_not_block_startup(tmp_path, monkeypatch) -> None:
    bot = _bot(tmp_path)

    async def slow_sync(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(bot.tree, "sync", slow_sync)
    monkeypatch.setattr(bot_module, "SYNC_TIMEOUT", 0.01)

    try:
        await bot._sync_commands()  # must not raise
    finally:
        await bot.close()


async def test_sync_http_error_does_not_block_startup(tmp_path, monkeypatch) -> None:
    bot = _bot(tmp_path)

    async def failing_sync(*args, **kwargs):
        response = SimpleNamespace(status=500, reason="Internal Server Error")
        raise discord.HTTPException(response, "boom")

    monkeypatch.setattr(bot.tree, "sync", failing_sync)

    try:
        await bot._sync_commands()  # must not raise
    finally:
        await bot.close()


async def test_unchanged_commands_are_not_resynced(tmp_path, monkeypatch) -> None:
    bot = _bot(tmp_path)
    calls = 0

    async def counting_sync(*args, **kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(bot.tree, "sync", counting_sync)

    try:
        await bot._sync_commands()
        await bot._sync_commands()
        assert calls == 1
    finally:
        await bot.close()


async def test_changed_commands_are_resynced(tmp_path, monkeypatch) -> None:
    bot = _bot(tmp_path)
    calls = 0

    async def counting_sync(*args, **kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(bot.tree, "sync", counting_sync)

    try:
        await bot._sync_commands()

        @bot.tree.command(name="brand-new", description="new command")
        async def brand_new(interaction: discord.Interaction) -> None:
            pass

        await bot._sync_commands()
        assert calls == 2
    finally:
        await bot.close()
