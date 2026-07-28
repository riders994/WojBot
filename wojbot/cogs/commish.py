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
Discord user to a manager, add a new manager, and migrate a league's existing
real Discord IDs to surrogates).

Discord IDs never reach the warehouse in the clear: ``link``/``linkuser``/
``adduser`` store an opaque **surrogate bigint** (via ``bot.discord_anon``) in
``dim_league.discord_server_id`` / ``dim_manager.discord_id`` and keep the real
value only bot-side, so the listings resolve a surrogate back to the real server
id / user mention while the DB holds only the opaque value. ``rename`` writes
``league_name`` (not anonymized) as a parameterized UPDATE. Adding a manager also
writes ``player_name``, which *is* anonymized by the elo package, so it goes
through EloSQL's own append/push (tokenizing and updating the reversal map)
rather than raw SQL.
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
from ..core.discord_anon import CATEGORY_MANAGER, CATEGORY_SERVER
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
        anon = self.bot.discord_anon
        lines = [
            f"`{int(lid)}` — **{name}** · server {_server_ref(anon, sid, interaction.guild_id)}"
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
        anon = self.bot.discord_anon
        lines = [
            f"`{int(mid)}` — **{name}** · discord {_manager_ref(anon, did)}"
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

        def _work():
            # Store an opaque surrogate, not the real guild id.
            surrogate = self.bot.discord_anon.surrogate(CATEGORY_SERVER, guild_id)
            return _update_column(
                elo_sql, "dim_league", "discord_server_id",
                surrogate, "league_id", league_id,
            )

        try:
            updated = await asyncio.to_thread(_work)
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
        def _work():
            # Store an opaque surrogate, not the real user id.
            surrogate = self.bot.discord_anon.surrogate(CATEGORY_MANAGER, user.id)
            return _update_column(
                elo_sql, "dim_manager", "discord_id",
                surrogate, "manager_id", manager_id,
            )

        try:
            updated = await asyncio.to_thread(_work)
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

    @db.command(name="adduser", description="Create a new manager in the database.")
    @app_commands.describe(
        name="The manager's name",
        user="Discord user to attach to the new manager (optional)",
    )
    @is_commissioner()
    async def db_adduser(
        self,
        interaction: discord.Interaction,
        name: str,
        user: discord.User | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        name = name.strip()
        if not name:
            await interaction.followup.send("A manager name is required.")
            return
        elo_sql = await self._db_backend(interaction)
        if elo_sql is None:
            return

        def _work():
            # Store an opaque surrogate for the discord id, not the real one.
            discord_id = (
                self.bot.discord_anon.surrogate(CATEGORY_MANAGER, user.id)
                if user is not None
                else None
            )
            # player_name is anonymized, so this goes through EloSQL's own
            # append/push (which tokenizes the name and rewrites the reversal
            # map) rather than a raw INSERT. Pull first so the minted id is off
            # the current table and existing tokens stay put.
            elo_sql.pull_dims("manager", overwrite=True)
            manager_id = elo_sql._next_dim_id("manager")
            elo_sql._append_dim(
                "manager",
                [{"manager_id": manager_id, "player_name": name, "discord_id": discord_id}],
            )
            elo_sql._push_dim("manager")
            return manager_id

        try:
            manager_id = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("db adduser failed")
            await interaction.followup.send(f"Couldn't add manager: `{exc}`")
            return
        suffix = ""
        if user is not None:
            suffix = f" and attached {user.mention} (`{user.id}`)"
        await interaction.followup.send(
            f"Added manager **{name}** as `manager_id` {manager_id}{suffix}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @db.command(
        name="migrate",
        description="Anonymize real Discord IDs already stored for a league.",
    )
    @app_commands.describe(league="League key to migrate (defaults to this server's)")
    @is_commissioner()
    async def db_migrate(
        self, interaction: discord.Interaction, league: str | None = None
    ) -> None:
        """One-off: replace any real Discord IDs a pre-surrogate build wrote for
        this league with surrogates, scoped to the named league.

        Detection is by column, not a value-range guess: manager discord_id seeds
        are alphanumeric (id_generator), so a real one is the numeric outlier;
        the numeric server id is confirmed against the bot's guilds. Values
        already surrogated (in the bot's map) are skipped, so it's re-runnable.
        """
        await interaction.response.defer(ephemeral=True)
        try:
            system = await get_or_build(self.bot, interaction.guild_id, league)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("migrate: load failed")
            await interaction.followup.send(f"Couldn't load league: `{exc}`")
            return
        if system.elo_sql is None:
            await interaction.followup.send(
                "No SQL backend is configured — check `sql_config.yml`."
            )
            return
        elo_sql = system.elo_sql
        schema = psql.Identifier(elo_sql.schema)

        def _gather():
            league_id = elo_sql.set_league_config(system.ratings_config)
            if league_id is None or league_id < 0:
                return None, None, []
            conn = elo_sql.conn
            with conn.cursor() as cur:
                cur.execute(
                    psql.SQL(
                        "SELECT discord_server_id FROM {s}.dim_league WHERE league_id = %s"
                    ).format(s=schema),
                    (league_id,),
                )
                got = cur.fetchone()
                server_id = got[0] if got else None
                # Managers in this league, by the teams they own in its seasons.
                cur.execute(
                    psql.SQL(
                        "SELECT DISTINCT m.manager_id, m.discord_id "
                        "FROM {s}.dim_manager m "
                        "JOIN {s}.dim_team t ON t.manager_id = m.manager_id "
                        "JOIN {s}.dim_online_league o "
                        "ON o.online_league_id = t.online_league_id "
                        "WHERE o.league_id = %s"
                    ).format(s=schema),
                    (league_id,),
                )
                managers = cur.fetchall()
            conn.rollback()  # read-only; end the transaction cleanly
            return league_id, server_id, managers

        try:
            league_id, server_id, managers = await asyncio.to_thread(_gather)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("migrate: gather failed")
            await interaction.followup.send(f"Migration read failed: `{exc}`")
            return
        if league_id is None:
            await interaction.followup.send(
                f"League `{system.league}` isn't in the database yet."
            )
            return

        anon = self.bot.discord_anon
        updates = []  # (table, column, surrogate, key_col, key_val)

        # Server id: numeric like its seed, so confirm it's a guild the bot is in.
        if not _is_missing(server_id):
            sid = int(server_id)
            if anon.real(CATEGORY_SERVER, sid) is None and self.bot.get_guild(sid) is not None:
                updates.append(
                    ("dim_league", "discord_server_id",
                     anon.surrogate(CATEGORY_SERVER, sid), "league_id", league_id)
                )

        # Manager discord_id: a real one is a numeric snowflake; seeds are the
        # alphanumeric id_generator strings.
        managers_migrated = 0
        for manager_id, discord_id in managers:
            if _is_missing(discord_id):
                continue
            value = str(discord_id).strip()
            if not value.isdigit() or not (15 <= len(value) <= 20):
                continue  # alphanumeric seed, or not a plausible id
            if anon.real(CATEGORY_MANAGER, value) is not None:
                continue  # already a surrogate we minted
            updates.append(
                ("dim_manager", "discord_id",
                 anon.surrogate(CATEGORY_MANAGER, int(value)), "manager_id", int(manager_id))
            )
            managers_migrated += 1

        if not updates:
            await interaction.followup.send(
                f"Nothing to migrate for `{system.league}` — no real Discord IDs found."
            )
            return

        def _apply():
            for table, column, surrogate, key_col, key_val in updates:
                _update_column(elo_sql, table, column, surrogate, key_col, key_val)

        try:
            await asyncio.to_thread(_apply)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("migrate: apply failed")
            await interaction.followup.send(f"Migration write failed: `{exc}`")
            return
        servers_migrated = sum(1 for u in updates if u[0] == "dim_league")
        await interaction.followup.send(
            f"Migrated `{system.league}`: anonymized {servers_migrated} server id "
            f"and {managers_migrated} manager discord id(s)."
        )


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and value != value)  # NaN


def _server_ref(anon, stored, current_guild_id) -> str:
    """Render a stored discord_server_id: real id when we know it, else opaque."""
    if _is_missing(stored):
        return "—"
    real = anon.real(CATEGORY_SERVER, stored)
    if real is None:
        return f"`{int(stored)}` (opaque)"
    if real == current_guild_id:
        return f"`{real}` (this server)"
    return f"`{real}`"


def _manager_ref(anon, stored) -> str:
    """Render a stored discord_id: a mention when we know it, else opaque."""
    if _is_missing(stored):
        return "— (unlinked)"
    real = anon.real(CATEGORY_MANAGER, stored)
    if real is None:
        return f"`{int(stored)}` (opaque)"
    return f"<@{real}>"


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
