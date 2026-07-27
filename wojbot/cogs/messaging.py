"""Messaging cog: the dad-joke listener, its per-server toggle, and /cat.

Ported from v1's Messaging cog (echo/add dropped for now). Two upgrades:
- the toggle is persisted per-server via ``bot.configs`` instead of v1's in-memory
  per-channel dict (which referenced a ``dadjoke_toggle`` attribute that never
  existed on the bot);
- dad-joke detection is a single regex helper rather than stacked ``.find()`` calls.

Reading message content requires the privileged ``message_content`` intent
(enabled in bot.py and in the Discord developer portal).
"""

from __future__ import annotations

import random
import re

import discord
from discord import app_commands
from discord.ext import commands

# Matches "I'm X" or "I am X" (straight or curly apostrophe), capturing the rest.
_DAD_PATTERN = re.compile(r"\bi(?:\s+am|['’]m)\s+(.+)", re.IGNORECASE)

CAT_GIFS = [
    "https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif",
    "https://media.giphy.com/media/vFKqnCdLPNOKc/giphy.gif",
    "https://media.giphy.com/media/mlvseq9yvZhba/giphy.gif",
]


def find_dad_target(text: str) -> str | None:
    """Return the phrase following "I'm"/"I am", or ``None`` if there's no match."""
    match = _DAD_PATTERN.search(text)
    if not match:
        return None
    return match.group(1).strip() or None


class Messaging(commands.Cog):
    """Dad jokes and other message-y silliness."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Turn dad jokes on/off for this server (omit to check).")
    @app_commands.describe(enabled="True to enable, False to disable. Omit to see the current setting.")
    @app_commands.guild_only()
    async def dadjokes(
        self, interaction: discord.Interaction, enabled: bool | None = None
    ) -> None:
        guild_id = interaction.guild_id
        if enabled is None:
            current = self.bot.configs.get_guild(guild_id)["dad_joke"]
            state = "on" if current else "off"
            await interaction.response.send_message(
                f"Dad jokes are currently **{state}** for this server."
            )
            return

        self.bot.configs.set_guild(guild_id, {"dad_joke": enabled})
        state = "on" if enabled else "off"
        await interaction.response.send_message(
            f"Dad jokes are now **{state}** for this server."
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self.bot.configs.get_guild(message.guild.id)["dad_joke"]:
            return
        target = find_dad_target(message.content)
        if target:
            await message.channel.send(f"Hi {target}, I'm dad")

    @app_commands.command(description="Get a random cat gif.")
    async def cat(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(random.choice(CAT_GIFS))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Messaging(bot))
