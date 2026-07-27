"""SQL cog: stands up the shared database service and owner-only admin commands.

On load it attaches a :class:`~wojbot.core.sql.SqlService` to the bot as
``bot.sql`` (other cogs — commish, elo, rumors — use it via ``bot.sql.execute``)
and opens the connection. Connection failure at startup is non-fatal: the service
and its ``/sql`` commands stay available so the DB can be brought up and
``/sql reconnect`` used, without restarting the bot.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..core.sql import SqlService

log = logging.getLogger(__name__)


class Sql(commands.Cog):
    """Database service owner + admin commands."""

    # Slash command group: /sql <status|reconnect>
    group = app_commands.Group(name="sql", description="Database admin (owner only).")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Build the service (loads the query registry — no DB needed) and try to
        # connect. A failed connect is logged, not raised, so the cog still loads.
        self.bot.sql = await asyncio.to_thread(SqlService.create)
        try:
            await self.bot.sql.connect()
            log.info("SQL connected: %s", await self.bot.sql.status())
        except Exception:
            log.exception("SQL connect failed at startup; use /sql reconnect once the DB is up")

    async def cog_unload(self) -> None:
        service = getattr(self.bot, "sql", None)
        if service is not None:
            await asyncio.to_thread(service.close)
            self.bot.sql = None

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if await self.bot.is_owner(interaction.user):
            return True
        await interaction.response.send_message("This command is owner-only.", ephemeral=True)
        return False

    @group.command(name="status", description="Show database connection status.")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._require_owner(interaction):
            return
        service = getattr(self.bot, "sql", None)
        if service is None:
            await interaction.response.send_message("SQL service is not loaded.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        status = await service.status()
        if status:
            lines = "\n".join(f"• `{n}`: {'up' if ok else 'down'}" for n, ok in status.items())
        else:
            lines = "No active connection."
        await interaction.followup.send(
            f"**SQL connections**\n{lines}\n\nRegistered queries: {len(service.queries)}"
        )

    @group.command(name="reconnect", description="Reconnect to the database.")
    async def reconnect(self, interaction: discord.Interaction) -> None:
        if not await self._require_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        service = getattr(self.bot, "sql", None)
        if service is None:
            self.bot.sql = service = await asyncio.to_thread(SqlService.create)
        try:
            await service.connect()
        except Exception as exc:  # noqa: BLE001 - surface the reason to the user
            await interaction.followup.send(f"Reconnect failed: {exc}")
            return
        await interaction.followup.send(f"Reconnected. Status: {await service.status()}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Sql(bot))
