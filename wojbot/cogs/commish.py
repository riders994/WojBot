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

The ``/commish db`` subgroup edits the SQL dimension tables directly (list
leagues/managers, link this server to a league, rename a league, attach a
Discord user to a manager). Those touch only the non-anonymized, "Discord-owned"
columns — ``dim_league.discord_server_id`` / ``league_name`` and
``dim_manager.discord_id`` — the same fields ``sync`` seeds but never overwrites.
They run as parameterized UPDATEs against the loaded system's connection.
"""

from __future__ import annotations

import asyncio
import logging

import discord
import yaml
from discord import app_commands
from discord.ext import commands
from psycopg2 import sql as psql

from ..core.checks import is_commissioner, is_privileged
from ..core.elo import (
    ELO_SYS_CONFIG,
    GUILD_LEAGUE_KEY,
    build_system,
    cached_system,
    configured_league,
    get_or_build,
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
    # Nested subgroup: /commish db <...> — direct edits to the SQL dim tables.
    db = app_commands.Group(
        name="db",
        description="Edit the league/manager tables in the database.",
        parent=group,
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

    # --- /commish db: direct SQL dim edits -------------------------------

    async def _db_backend(self, interaction: discord.Interaction):
        """Resolve this guild's Elo SQL backend, or report why it's unavailable.

        Auto-builds the default system if none is loaded — these edits are
        global (dim_league / dim_manager span every league), so they only need a
        connection and the schema, not a league bound to this server.
        """
        try:
            system = await get_or_build(self.bot, interaction.guild_id)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("Failed to resolve Elo system")
            await interaction.followup.send(f"Couldn't reach the league backend: `{exc}`")
            return None
        if system.elo_sql is None:
            await interaction.followup.send(
                "No SQL backend is configured — check `sql_config.yml`."
            )
            return None
        return system.elo_sql

    @db.command(name="leagues", description="List the leagues in the database.")
    @is_commissioner()
    async def db_leagues(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        elo_sql = await self._db_backend(interaction)
        if elo_sql is None:
            return

        def _work():
            elo_sql.pull_dims("league", overwrite=True)
            frame = elo_sql.dim_tables["dim_league"].reset_index()
            cols = ["league_id", "league_name", "discord_server_id"]
            return frame[cols].values.tolist()

        try:
            rows = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("db leagues failed")
            await interaction.followup.send(f"Couldn't read leagues: `{exc}`")
            return
        if not rows:
            await interaction.followup.send("No leagues in the database yet.")
            return
        lines = [
            f"`{int(lid)}` — **{name}** · server `{_bigint(sid)}`"
            for lid, name, sid in rows
        ]
        await interaction.followup.send("**Leagues**\n" + "\n".join(lines))

    @db.command(name="managers", description="List managers in the database.")
    @app_commands.describe(search="Only show managers whose name matches this")
    @is_commissioner()
    async def db_managers(
        self, interaction: discord.Interaction, search: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        elo_sql = await self._db_backend(interaction)
        if elo_sql is None:
            return

        def _work():
            elo_sql.pull_dims("manager", overwrite=True)
            frame = elo_sql.dim_tables["dim_manager"].reset_index()
            if search:
                names = frame["player_name"].astype(str)
                frame = frame[names.str.contains(search, case=False, na=False, regex=False)]
            cols = ["manager_id", "player_name", "discord_id"]
            return frame[cols].values.tolist()

        try:
            rows = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("db managers failed")
            await interaction.followup.send(f"Couldn't read managers: `{exc}`")
            return
        if not rows:
            await interaction.followup.send("No matching managers.")
            return
        shown = rows[:25]
        lines = [
            f"`{int(mid)}` — **{name}** · discord {_discord_ref(did)}"
            for mid, name, did in shown
        ]
        footer = f"\n…and {len(rows) - 25} more (narrow with `search`)." if len(rows) > 25 else ""
        await interaction.followup.send("**Managers**\n" + "\n".join(lines) + footer)

    @db.command(name="link", description="Link THIS server to a league in the database.")
    @app_commands.describe(league_id="Target league_id (see /commish db leagues)")
    @is_commissioner()
    async def db_link(self, interaction: discord.Interaction, league_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        elo_sql = await self._db_backend(interaction)
        if elo_sql is None:
            return
        guild_id = interaction.guild_id
        try:
            updated = await asyncio.to_thread(
                _update_column, elo_sql, "dim_league", "discord_server_id",
                guild_id, "league_id", league_id,
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("db link failed")
            await interaction.followup.send(f"Link failed: `{exc}`")
            return
        if not updated:
            await interaction.followup.send(f"No league with `league_id` {league_id}.")
            return
        await interaction.followup.send(
            f"Linked this server (`{guild_id}`) to league `{league_id}`.\n"
            "To also drive this league from the bot, a commissioner can run "
            "`/commish load <league>`."
        )

    @db.command(name="rename", description="Rename a league in the database.")
    @app_commands.describe(
        league_id="Target league_id (see /commish db leagues)", name="New league name"
    )
    @is_commissioner()
    async def db_rename(
        self, interaction: discord.Interaction, league_id: int, name: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        elo_sql = await self._db_backend(interaction)
        if elo_sql is None:
            return
        try:
            updated = await asyncio.to_thread(
                _update_column, elo_sql, "dim_league", "league_name",
                name, "league_id", league_id,
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("db rename failed")
            await interaction.followup.send(f"Rename failed: `{exc}`")
            return
        if not updated:
            await interaction.followup.send(f"No league with `league_id` {league_id}.")
            return
        await interaction.followup.send(f"Renamed league `{league_id}` to **{name}**.")

    @db.command(name="linkuser", description="Attach a Discord user to a manager.")
    @app_commands.describe(
        manager_id="Target manager_id (see /commish db managers)",
        user="The Discord user to attach",
    )
    @is_commissioner()
    async def db_linkuser(
        self, interaction: discord.Interaction, manager_id: int, user: discord.User
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        elo_sql = await self._db_backend(interaction)
        if elo_sql is None:
            return
        try:
            updated = await asyncio.to_thread(
                _update_column, elo_sql, "dim_manager", "discord_id",
                user.id, "manager_id", manager_id,
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("db linkuser failed")
            await interaction.followup.send(f"Link failed: `{exc}`")
            return
        if not updated:
            await interaction.followup.send(f"No manager with `manager_id` {manager_id}.")
            return
        await interaction.followup.send(
            f"Attached {user.mention} (`{user.id}`) to manager `{manager_id}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _bigint(value) -> str:
    """Render a possibly-null bigint id for display."""
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return "—"
    return str(int(value))


def _discord_ref(value) -> str:
    """Render a manager's discord_id as a mention when set, else a dash."""
    rendered = _bigint(value)
    return f"<@{rendered}>" if rendered != "—" else "— (unlinked)"


def _update_column(elo_sql, table, column, value, key_col, key_val) -> int:
    """Run ``UPDATE <schema>.<table> SET <column>=%s WHERE <key_col>=%s``.

    Identifiers (schema/table/column) come from our own constants and are quoted
    via ``psycopg2.sql``; the value and key are bound as parameters. Returns the
    number of rows updated (0 means the key wasn't found).
    """
    query = psql.SQL("UPDATE {}.{} SET {} = %s WHERE {} = %s").format(
        psql.Identifier(elo_sql.schema),
        psql.Identifier(table),
        psql.Identifier(column),
        psql.Identifier(key_col),
    )
    conn = elo_sql.conn
    with conn.cursor() as cur:
        cur.execute(query, (value, key_val))
        updated = cur.rowcount
    conn.commit()
    return updated


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Commish(bot))
