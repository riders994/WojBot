"""The WojBot v2 bot class."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .cogs import discover_extensions
from .core.config import PROJECT_ROOT, Settings
from .core.config_store import ConfigStore
from .core.data import LeagueCache, LocalSource, Runtime

log = logging.getLogger(__name__)


class WojBotV2(commands.Bot):
    """Slash-command Discord bot for a fantasy basketball league."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        # Storage layers (see wojbot/core):
        #  - configs: persistent bot/guild/league settings (flat YAML, cached).
        #  - leagues: in-memory cache of authoritative league data. Uses the local
        #    source for now; the SQL-backed source (via bot.sql / rv_pytools) is
        #    wired in with the commish/elo cogs.
        #  - runtime: registry for live, non-persisted objects (scraper, elo engine).
        # The database connection itself is provided by the sql cog as bot.sql.
        self.configs = ConfigStore.load()
        self.leagues = LeagueCache(LocalSource(PROJECT_ROOT / "resources"))
        self.runtime = Runtime()

        # Slash commands only need the default intents; message_content is the
        # privileged intent the messaging cog's dad-joke listener needs to read
        # message text. It must also be enabled in the Discord developer portal
        # (Bot > Privileged Gateway Intents > Message Content Intent).
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,  # unused (slash-only), harmless
            intents=intents,
            help_command=None,
        )
        # Turn failed permission checks (and other app-command errors) into a
        # friendly ephemeral reply instead of a silent failure / raw traceback.
        self.tree.on_error = self._on_app_command_error

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        if isinstance(error, discord.app_commands.CheckFailure):
            message = "You don't have permission to use this command."
        else:
            log.exception("Unhandled app command error", exc_info=error)
            message = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def setup_hook(self) -> None:
        """Load all discovered cogs, then sync the application command tree."""
        for extension in discover_extensions():
            try:
                await self.load_extension(extension)
                log.info("Loaded extension %s", extension)
            except Exception:
                # One bad cog shouldn't stop the whole bot from starting.
                log.exception("Failed to load extension %s", extension)

        if self.settings.guild_id is not None:
            guild = discord.Object(id=self.settings.guild_id)
            # Copy global commands to the dev guild for instant availability;
            # global sync can take up to ~1 hour to propagate.
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to guild %s", len(synced), self.settings.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global command(s)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
