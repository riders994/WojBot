"""Meta cog: health check and bot info.

This is the first v2 cog and the template every ported cog should follow —
a ``commands.Cog`` subclass with ``@app_commands.command()`` slash commands and
an ``async def setup(bot)`` entry point.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from wojbot import __version__


class Meta(commands.Cog):
    """Basic health and info commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Check that the bot is alive and see its latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! ({latency_ms} ms)")

    @app_commands.command(description="Show basic information about the bot.")
    async def about(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"**WojBot** v{__version__} — a fantasy basketball league bot."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Meta(bot))
