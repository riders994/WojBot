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
        #    source for now; swap in PostgresSource(settings.database_url) once the
        #    SQL source is implemented with the commish/elo cogs.
        #  - runtime: registry for live, non-persisted objects (scraper, elo engine).
        self.configs = ConfigStore.load()
        self.leagues = LeagueCache(LocalSource(PROJECT_ROOT / "resources"))
        self.runtime = Runtime()

        # Default intents are enough for slash commands. Future cogs that need
        # privileged intents (e.g. a dad-joke listener needs message_content)
        # should enable them here and in the Discord developer portal.
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,  # unused (slash-only), harmless
            intents=intents,
            help_command=None,
        )

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
