"""Commish cog: commissioner tools backed by the ``elo-system`` package.

Replaces v1's Yahoo-driven ``League`` cog. The heavy lifting — scraping a fantasy
platform, computing Elo ratings, publishing them to CSV or Postgres — lives in the
``elo_system`` package (github.com/riders994/eloSystem). This cog is the Discord
surface: pick a league, load its configs, sync member/team dimensions, publish
ratings, and back up configs.

Config lives alongside the bot's own settings in ``resources/configs/``: a shared
``sys_config.yml`` names the leagues, with a per-league ``ratings_*.yml`` plus the
shared ``csv_config.yml`` / ``sql_config.yml``. There is one :class:`EloSystem`
per league, cached in ``bot.runtime`` under ``elo:<league>``.

The Elo SQL backend reuses the bot's live warehouse connection (``bot.sql``)
rather than opening its own — same database, one connection. Because that
connection is shared outside :class:`~wojbot.core.sql.SqlService`'s lock, the
mutating commands here defer and run one operation at a time.

Tiering: read-only ``/commish leagues|status`` need admin/privileged access;
the mutating ``/commish load|sync|publish|dump`` are commissioner-only.
"""

from __future__ import annotations

import asyncio
import logging

import discord
import yaml
from discord import app_commands
from discord.ext import commands

from ..core.checks import is_commissioner, is_privileged
from ..core.elo import (
    ELO_SYS_CONFIG,
    GUILD_LEAGUE_KEY,
    build_system,
    cached_system,
    configured_league,
    runtime_key,
)

log = logging.getLogger(__name__)


class Commish(commands.Cog):
    """Commissioner tools for the league's Elo backend."""

    group = app_commands.Group(
        name="commish",
        description="Commissioner tools for the league's Elo ratings.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --- league selection / construction (shared: wojbot.core.elo) --------

    def _configured_league(self, guild_id: int | None) -> str | None:
        return configured_league(self.bot, guild_id)

    async def _build_system(self, league: str | None):
        return await build_system(self.bot, league)

    def _loaded_system(self, guild_id: int | None):
        return cached_system(self.bot, guild_id)

    def _describe(self, system, *, loaded: bool = False) -> str:
        league = system.league or "(default)"
        ratings = system.ratings_config or {}
        name = ratings.get("league_name", league)
        seasons = ", ".join(str(y) for y in sorted(ratings.get("seasons", {}))) or "(none)"
        dynasty = "yes" if ratings.get("is_dynasty") else "no"
        sql = "yes" if system.elo_sql is not None else "no"
        header = "Loaded" if loaded else "League"
        return (
            f"**{header}: {name}** (`{league}`)\n"
            f"• Seasons: {seasons}\n"
            f"• Dynasty: {dynasty}\n"
            f"• Reader / Writer: `{system.reader_key}` / `{system.writer_key}`\n"
            f"• SQL backend: {sql}"
        )

    # --- read-only (privileged) ------------------------------------------

    @group.command(name="leagues", description="List the configured Elo leagues.")
    @is_privileged()
    async def leagues(self, interaction: discord.Interaction) -> None:
        try:
            cfg = yaml.safe_load(ELO_SYS_CONFIG.read_text()) or {}
        except FileNotFoundError:
            await interaction.response.send_message(
                "No `sys_config.yml` found in `resources/configs`.", ephemeral=True
            )
            return
        configured = cfg.get("ratings_configs", {})
        default = cfg.get("default_league")
        bound = self._configured_league(interaction.guild_id)
        lines = []
        for key in sorted(configured):
            tags = []
            if key == bound:
                tags.append("this server")
            if key == default:
                tags.append("default")
            suffix = f" — {', '.join(tags)}" if tags else ""
            lines.append(f"• `{key}`{suffix}")
        body = "\n".join(lines) or "(none configured)"
        await interaction.response.send_message(
            f"**Configured leagues**\n{body}", ephemeral=True
        )

    @group.command(name="status", description="Show the league loaded for this server.")
    @is_privileged()
    async def status(self, interaction: discord.Interaction) -> None:
        system = self._loaded_system(interaction.guild_id)
        if system is None:
            await interaction.response.send_message(
                "No league is loaded — run `/commish load` first.", ephemeral=True
            )
            return
        await interaction.response.send_message(self._describe(system), ephemeral=True)

    # --- mutating (commissioner) -----------------------------------------

    @group.command(
        name="load", description="Load a league's Elo system and bind it to this server."
    )
    @app_commands.describe(
        league="League key from sys_config (defaults to this server's, then the system default)"
    )
    @is_commissioner()
    async def load(
        self, interaction: discord.Interaction, league: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        requested = league or self._configured_league(interaction.guild_id)
        try:
            system = await self._build_system(requested)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("Failed to load Elo system")
            await interaction.followup.send(f"Couldn't load league: `{exc}`")
            return
        concrete = system.league
        self.bot.runtime.set(runtime_key(concrete), system)
        if concrete:
            self.bot.configs.set_guild(interaction.guild_id, {GUILD_LEAGUE_KEY: concrete})
        await interaction.followup.send(self._describe(system, loaded=True))

    @group.command(
        name="sync", description="Sync members & teams to the database (scrapes each season)."
    )
    @is_commissioner()
    async def sync(self, interaction: discord.Interaction) -> None:
        system = self._loaded_system(interaction.guild_id)
        if system is None:
            await interaction.response.send_message(
                "No league is loaded — run `/commish load` first.", ephemeral=True
            )
            return
        if system.elo_sql is None:
            await interaction.response.send_message(
                "No SQL backend is configured — check `sql_config.yml`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(system.sync_dims)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("sync_dims failed")
            await interaction.followup.send(f"Sync failed: `{exc}`")
            return
        self.bot.leagues.invalidate(interaction.guild_id)
        await interaction.followup.send(
            f"Synced members & teams for `{system.league}`."
        )

    @group.command(name="publish", description="Run and publish the league's Elo ratings.")
    @is_commissioner()
    async def publish(self, interaction: discord.Interaction) -> None:
        system = self._loaded_system(interaction.guild_id)
        if system is None:
            await interaction.response.send_message(
                "No league is loaded — run `/commish load` first.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        def _run():
            league = system.elo_league
            if league is None:
                raise RuntimeError("League config not loaded")
            if not league.loaded:
                league.load()
            # is_dynasty settles whether a season stands alone or feeds the
            # cross-season dynasty timeline; run the matching pipeline.
            if league.is_dynasty:
                league.run_dynasty()
            else:
                league.run_season()
            return system.publish()

        try:
            await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("publish failed")
            await interaction.followup.send(f"Publish failed: `{exc}`")
            return
        self.bot.leagues.invalidate(interaction.guild_id)
        await interaction.followup.send(
            f"Published Elo ratings for `{system.league}` via `{system.writer_key}`."
        )

    @group.command(name="dump", description="Back up the league's config files.")
    @is_commissioner()
    async def dump(self, interaction: discord.Interaction) -> None:
        system = self._loaded_system(interaction.guild_id)
        if system is None:
            await interaction.response.send_message(
                "No league is loaded — run `/commish load` first.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            written = await asyncio.to_thread(system.dump)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("config dump failed")
            await interaction.followup.send(f"Backup failed: `{exc}`")
            return
        sections = ", ".join(f"`{k}`" for k in written) or "(nothing)"
        await interaction.followup.send(f"Backed up configs: {sections}.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Commish(bot))
