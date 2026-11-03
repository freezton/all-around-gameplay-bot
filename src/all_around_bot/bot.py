"""Bot wiring: intents, extension loading, lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pkgutil
import signal
from contextlib import suppress
from pathlib import Path

import discord
from discord.ext import commands

from . import cogs
from .config import Settings
from .db import MessageStore

log = logging.getLogger(__name__)

SYNC_TIMEOUT = 20.0
SYNC_HASH_FILE = "commands.sha256"


class AllAroundBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        # Privileged: enable "Message Content Intent" in the Developer Portal or
        # messages arrive with empty content. We deliberately do NOT request
        # `members` — nothing here needs the member list, and requesting an intent
        # that is disabled in the portal makes the whole gateway connection fail
        # with PrivilegedIntentsRequired.
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, replied_user=True
            ),
        )
        self.settings = settings
        self.store = MessageStore(settings.database_path)
        self.tree.on_error = self._on_app_command_error

    async def setup_hook(self) -> None:
        await self.store.open()
        await self._load_cogs()
        await self._sync_commands()
        self._install_signal_handlers()

    async def _load_cogs(self) -> None:
        for info in pkgutil.iter_modules(cogs.__path__, prefix=f"{cogs.__name__}."):
            await self.load_extension(info.name)
            log.info("Loaded extension %s", info.name)

    def _sync_state_path(self) -> Path:
        return self.settings.database_path.parent / SYNC_HASH_FILE

    def _command_tree_hash(self, guild: discord.abc.Snowflake | None) -> str:
        commands = self.tree.get_commands(guild=guild)
        payload = sorted(
            (command.to_dict(self.tree) for command in commands),
            key=lambda data: (data["type"], data["name"]),
        )
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob + str(getattr(guild, "id", "global")).encode()).hexdigest()

    def _read_sync_hash(self) -> str | None:
        with suppress(OSError):
            return self._sync_state_path().read_text().strip()
        return None

    def _write_sync_hash(self, digest: str) -> None:
        with suppress(OSError):
            path = self._sync_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(digest)

    async def _sync_commands(self) -> None:
        guild = (
            discord.Object(id=self.settings.dev_guild_id)
            if self.settings.dev_guild_id is not None
            else None
        )
        if guild is not None:
            self.tree.copy_global_to(guild=guild)

        # Re-registering identical commands bumps their Discord-side version on
        # every restart, which makes clients treat their cached copy as stale
        # ("Command is not available. Try again in 5 minutes."). It also hits
        # Discord's tiny rate limit for bulk command overwrites. Only sync when
        # the command tree actually changed.
        digest = self._command_tree_hash(guild)
        if self._read_sync_hash() == digest:
            log.info("Application commands unchanged; skipping sync")
            return

        log.info("Syncing application commands…")
        try:
            synced = await asyncio.wait_for(self.tree.sync(guild=guild), timeout=SYNC_TIMEOUT)
        except TimeoutError:
            # Never let a slow API call keep the bot offline.
            log.warning(
                "Command sync timed out after %ss; starting with the commands Discord already has",
                SYNC_TIMEOUT,
            )
            return
        except discord.HTTPException as exc:
            log.warning("Command sync failed (%s); starting anyway", exc)
            return

        if guild is not None:
            log.info("Synced %d commands to dev guild %s", len(synced), guild.id)
        else:
            log.info("Synced %d global commands", len(synced))
        self._write_sync_hash(digest)

    def _install_signal_handlers(self) -> None:
        loop = self.loop
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, lambda: loop.create_task(self.close()))

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info(
            "Logged in as %s (%s) in %d guild(s)",
            self.user,
            self.user.id,
            len(self.guilds),
        )

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        if isinstance(error, discord.app_commands.CommandInvokeError):
            error = error.original

        command = interaction.command
        log.error(
            "Command /%s failed",
            command.name if command else "<unknown>",
            exc_info=error,
        )

        message = f"Something went wrong: `{type(error).__name__}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def close(self) -> None:
        log.info("Shutting down")
        await self.store.close()
        await super().close()
