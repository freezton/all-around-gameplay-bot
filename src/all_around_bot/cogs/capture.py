"""Collect one user's messages: live listener plus a history backfill command."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..db import StoredMessage

if TYPE_CHECKING:
    from ..bot import AllAroundBot

log = logging.getLogger(__name__)

BATCH_SIZE = 500


def _to_stored(message: discord.Message) -> StoredMessage:
    return StoredMessage(
        message_id=message.id,
        guild_id=message.guild.id if message.guild else 0,
        channel_id=message.channel.id,
        author_id=message.author.id,
        author_name=message.author.display_name,
        content=message.clean_content,
        created_at=message.created_at,
    )


def _can_read(channel: discord.abc.GuildChannel, me: discord.Member) -> bool:
    """True when the bot can actually see the channel and walk its history."""
    permissions = channel.permissions_for(me)
    return permissions.view_channel and permissions.read_message_history


class Capture(commands.Cog):
    """Keeps the message archive in sync with the server."""

    def __init__(self, bot: AllAroundBot) -> None:
        self.bot = bot

    def _should_capture(self, author_id: int, guild_id: int | None) -> bool:
        """Everything is archived unless ``TARGET_USER_ID`` narrows it down."""
        target = self.bot.settings.target_user_id
        if target is not None and author_id != target:
            return False

        guild = self.bot.settings.target_guild_id
        return guild is None or guild_id == guild

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not message.content.strip():
            return
        if not self._should_capture(message.author.id, message.guild.id):
            return

        await self.bot.store.add_many([_to_stored(message)])

    @app_commands.command(
        name="collect",
        description="Import this server's message history (everyone, or one user).",
    )
    @app_commands.describe(user="Only import this user's messages (default: everyone)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def collect(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        settings = self.bot.settings

        # None means "everybody".
        target_id = user.id if user else None

        if settings.target_guild_id is not None and guild.id != settings.target_guild_id:
            await interaction.response.send_message(
                "This server is not the configured `TARGET_GUILD_ID`.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        # Private channels are included automatically as long as the bot's role
        # resolves to View Channel + Read Message History there.
        text_channels = guild.text_channels
        readable = [channel for channel in text_channels if _can_read(channel, guild.me)]
        readable += [thread for thread in guild.threads if _can_read(thread, guild.me)]

        if not readable:
            await interaction.followup.send(
                f"I can't read any of the {len(text_channels)} text channels here. "
                "Check that my role has **View Channel** and **Read Message History**, "
                "both in the channel and in its category.",
                ephemeral=True,
            )
            return

        channels = readable
        unreadable = len(text_channels) - sum(
            1 for channel in readable if not isinstance(channel, discord.Thread)
        )

        imported = 0
        scanned = 0
        skipped: list[str] = []
        batch: list[StoredMessage] = []

        for index, channel in enumerate(channels, start=1):
            try:
                async for message in channel.history(limit=None):
                    scanned += 1
                    if message.author.bot:
                        continue
                    if target_id is not None and message.author.id != target_id:
                        continue
                    if not message.content.strip():
                        continue

                    batch.append(_to_stored(message))
                    if len(batch) >= BATCH_SIZE:
                        imported += await self.bot.store.add_many(batch)
                        batch.clear()
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Skipping #%s: %s", channel.name, exc)
                skipped.append(channel.name)

            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(
                    content=(
                        f"Scanning… {index}/{len(channels)} channels, "
                        f"{scanned} messages seen, {imported} imported."
                    )
                )

        imported += await self.bot.store.add_many(batch)

        scope = f"<@{target_id}>" if target_id is not None else "everyone"
        summary = (
            f"Done. **{imported}** new messages archived for {scope} "
            f"(scanned {scanned} across {len(channels)} channels)."
        )
        if unreadable:
            summary += f"\n**{unreadable}** text channel(s) were skipped because I can't see them."
        if skipped:
            summary += f"\nSkipped: {', '.join(f'#{name}' for name in skipped)}"
        await interaction.edit_original_response(content=summary)

    @app_commands.command(name="stats", description="Show how many messages are archived.")
    @app_commands.describe(user="Only count this user's messages")
    async def stats(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        settings = self.bot.settings

        if user is not None:
            total = await self.bot.store.count(user.id, settings.target_guild_id)
            text = f"Archived **{total}** messages for {user.mention}."
        else:
            total = await self.bot.store.count(None, settings.target_guild_id)
            authors = await self.bot.store.author_count(settings.target_guild_id)
            text = f"Archived **{total}** messages from **{authors}** users."

        await interaction.response.send_message(text, ephemeral=True)


async def setup(bot: AllAroundBot) -> None:
    await bot.add_cog(Capture(bot))
