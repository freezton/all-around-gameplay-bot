"""Fun commands: render random archived messages as images."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..render import render_message_card

if TYPE_CHECKING:
    from ..bot import AllAroundBot

log = logging.getLogger(__name__)


class Fun(commands.Cog):
    def __init__(self, bot: AllAroundBot) -> None:
        self.bot = bot
        self._avatars: dict[int, bytes | None] = {}

    @app_commands.command(
        name="ping",
        description="Post a random archived message (optionally from one user).",
    )
    @app_commands.describe(
        user="Only pick messages from this user",
        reply_original="Reply to the original message (or link it in another channel)",
    )
    async def ping(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        reply_original: bool = False,
    ) -> None:
        settings = self.bot.settings
        await interaction.response.defer(thinking=True)

        message = await self.bot.store.random_for(
            user.id if user else None, settings.target_guild_id
        )
        if message is None:
            scope = f" from {user.mention}" if user else ""
            await interaction.followup.send(
                f"Nothing archived{scope} yet — run `/collect` first.", ephemeral=True
            )
            return

        avatar = await self._avatar(message.author_id)
        png = await asyncio.to_thread(render_message_card, message, avatar=avatar)
        file = discord.File(io.BytesIO(png), filename="message.png")

        channel = interaction.channel
        if not reply_original or channel is None:
            await interaction.followup.send(file=file)
            return

        same_channel = (
            interaction.guild_id == message.guild_id
            and interaction.channel_id == message.channel_id
        )

        if same_channel:
            # Discord only supports replies within the same channel.
            reference = discord.MessageReference(
                message_id=message.message_id,
                channel_id=message.channel_id,
                guild_id=message.guild_id,
                fail_if_not_exists=False,
            )
            await channel.send(file=file, reference=reference, mention_author=False)
        else:
            await channel.send(content=f"<{message.jump_url}>", file=file)

        # Drop the "thinking…" placeholder; the card lives in the channel now.
        with contextlib.suppress(discord.HTTPException):
            await interaction.delete_original_response()

    async def _avatar(self, user_id: int) -> bytes | None:
        if user_id in self._avatars:
            return self._avatars[user_id]

        data: bytes | None = None
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            data = await user.display_avatar.read()
        except (discord.HTTPException, discord.NotFound):
            log.debug("Could not fetch avatar for %s", user_id, exc_info=True)

        self._avatars[user_id] = data
        return data


async def setup(bot: AllAroundBot) -> None:
    await bot.add_cog(Fun(bot))
