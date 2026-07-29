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

Tiering: read-only ``/commish leagues|status|season list`` need admin/privileged
access; the mutating ``/commish load|sync|publish|dump`` are commissioner-only.

The ``/commish season`` subgroup owns the league's seasons — its league-years.
``add`` registers one more (the platform id, scraped on the spot, which is what
validates it) and writes it back to the league's ``ratings_*.yml``; ``current``
settles which season the commands fall back to when none is named; ``platform``
records which platform a season was played on; ``list`` shows what is on file. A
season only reaches the database on the next ``sync``/``publish``, since
publishing re-reads the config and syncs the dims before it writes any ratings.

Moving the league to another platform is a config edit plus ``/commish load``,
which builds a fresh :class:`EloSystem` — and so a fresh scraper — off the
league's ``platform``. The dims key an account by ``(platform, account id)`` and
read the platform *per season*, defaulting to the league's, so the move mints a
``dim_manager_platform`` row (and a manager behind it) for each account on the
new platform. That is only correct for the seasons actually played there: an
unpinned past season follows the league to the new platform and has its accounts
re-read as new ones. ``/commish season platform`` pins history first; ``add``
pins each season as it goes.

The ``/commish db`` subgroup edits the SQL dimension tables directly. The
intended onboarding order for an existing SQL league is: ``migrate`` (adopt the
league for this server — binds the server, storing a surrogate for its id), then
``managers`` (see the roster with platform names), then ``linkuser`` / ``adduser``
(attach Discord users one at a time). ``leagues`` / ``rename`` / ``link`` round
out the league-level edits.

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
from ..core.format import clip as _clip

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
    # Nested subgroup: /commish season <...> — the league's seasons (league-years).
    season = app_commands.Group(
        name="season",
        description="Add and manage the league's seasons.",
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

    # --- /commish season: the league's seasons (league-years) ------------

    async def _league(self, interaction: discord.Interaction):
        """Resolve (building if needed) this guild's EloLeague, loaded.

        Returns ``(system, league)``, or ``(None, None)`` after reporting why
        not. Loading only reads the config — the scrape is the caller's to do.
        """
        try:
            system = await get_or_build(self.bot, interaction.guild_id)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("Failed to resolve Elo system")
            await interaction.followup.send(f"Couldn't load this server's league: `{exc}`")
            return None, None
        league = system.elo_league
        if league is None:
            await interaction.followup.send(
                "That league has no ratings config — check `resources/configs`."
            )
            return None, None
        if not league.loaded:
            league.load()
        return system, league

    @season.command(name="list", description="List the seasons configured for this league.")
    @is_privileged()
    async def season_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        system, league = await self._league(interaction)
        if league is None:
            return
        if not league.seasons:
            await interaction.followup.send(
                f"`{system.league}` has no seasons configured — add one with "
                "`/commish season add`."
            )
            return

        lines = []
        for year in sorted(league.seasons):
            config = league.seasons[year]
            tags = []
            if year == league.current_sports_year:
                tags.append("current")
            if config.get("current_season_length") is None:
                # No length on file means the season has never been scraped, so
                # it has no members and nothing can be rated for it yet.
                tags.append("not scraped")
            platform = config.get("platform")
            if platform is None:
                # Nothing pinned, so this season answers to whatever the league
                # currently says — which moves under it if the league moves.
                tags.append(f"inherits `{league.platform}`")
                platform = league.platform
            suffix = f" — {', '.join(tags)}" if tags else ""
            members = len(config.get("league_members") or {})
            lines.append(
                f"• **{year}** `{config.get('league_id')}` on `{platform}` · "
                f"{members} manager(s) · {config.get('current_season_length', 0)} week(s){suffix}"
            )
        name = (system.ratings_config or {}).get("league_name", system.league)
        unpinned = [y for y in league.seasons if league.seasons[y].get("platform") is None]
        if unpinned:
            lines.append(
                f"\n{len(unpinned)} season(s) name no platform of their own. Pin them with "
                "`/commish season platform` before moving the league to another platform, "
                "or they follow it and their managers are re-read as new accounts."
            )
        await interaction.followup.send(
            _clip(f"**{name} — seasons**\n" + "\n".join(lines))
        )

    @season.command(name="add", description="Add a new season (league-year) to this league.")
    @app_commands.describe(
        year="The season's year, e.g. 2026",
        league_id="The league's id on the platform for that season",
        sync="Also register the season's managers & teams in the database",
        overwrite="Replace the season if it is already configured",
    )
    @is_commissioner()
    async def season_add(
        self,
        interaction: discord.Interaction,
        year: int,
        league_id: str,
        sync: bool = False,
        overwrite: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        league_id = league_id.strip()
        if not league_id:
            await interaction.followup.send("A platform league id is required.")
            return
        system, league = await self._league(interaction)
        if league is None:
            return
        if year in league.seasons and not overwrite:
            await interaction.followup.send(
                f"Season {year} is already configured as "
                f"`{league.seasons[year].get('league_id')}`. Pass `overwrite: True` to "
                "replace it — its members would be re-read from the platform."
            )
            return
        if sync and system.elo_sql is None:
            await interaction.followup.send(
                "No SQL backend is configured — check `sql_config.yml`, or add the "
                "season without `sync`."
            )
            return

        def _work():
            # add_season registers the season and then scrapes it, which is what
            # proves the id is real — so a bad one fails here rather than at the
            # next publish. A failed scrape leaves the season half-registered,
            # so it is put back the way it was before the error travels on.
            previous = league.seasons.get(year)
            # add_season scrapes through add_league, which skips a year it has
            # already built. Replacing a season would then keep the old scrape
            # and leave the new id unread, so the cached one goes first.
            league.remove_league(year)
            try:
                # Pin the platform this season is played on. The dims read it
                # per season (falling back to the league's), so pinning it now
                # is what keeps this season attributed to the platform it was
                # actually played on if the league later moves to another.
                season_config = {"league_id": league_id, "platform": league.platform}
                if not league.add_season(season_config, year, league=True):
                    raise ValueError(
                        f"`{league_id}` isn't a usable season for a "
                        f"{league.platform} league."
                    )
            except Exception:
                league.remove_league(year)
                if previous is None:
                    league.seasons.pop(year, None)
                else:
                    league.seasons[year] = previous
                raise
            # dump() folds the scraped season into system.ratings_config (the
            # same dict), which write_configs then puts on disk.
            league.dump()
            system.write_configs("ratings")
            # A FrameManager built by an earlier command captured the seasons
            # dict as it was, and is only ever rebuilt when there is none — so
            # it would not know this season exists, and rating it would fail
            # with "Unknown season". Point it at the live one, which is what
            # dump() just made config['seasons'] and what a fresh manager would
            # take. Repointing rather than dropping keeps any ratings already
            # loaded or run in this process.
            if league.frame_manager is not None:
                league.frame_manager.config = league.seasons
            if sync:
                # Already scraped just now, so don't do it again for every season.
                system.sync_dims(years=[year], scrape=False)
            return league.seasons[year]

        try:
            config = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("season add failed")
            await interaction.followup.send(f"Couldn't add season {year}: `{exc}`")
            return
        self.bot.leagues.invalidate(interaction.guild_id)

        members = config.get("league_members") or {}
        lines = [
            f"Added season **{year}** to `{system.league}` (`{league_id}`).",
            f"• Name on {league.platform}: **{config.get('league_name') or '—'}**",
            f"• Scored weeks: {config.get('current_season_length', 0)}",
            f"• Playoffs start: week {config.get('playoff_start', '—')}",
            f"• Managers: {len(members)}",
        ]
        roster = [
            str(info.get("curr_name") or info.get("short_name") or member_id)
            for member_id, info in list(members.items())[:25]
        ]
        if roster:
            more = f" …and {len(members) - 25} more" if len(members) > 25 else ""
            lines.append("  " + ", ".join(roster) + more)
        if not sync:
            lines.append(
                "\nIts managers and teams reach the database on the next "
                "`/commish publish` (or now, with `/commish sync`)."
            )
        if year > (league.current_sports_year or 0):
            lines.append(
                f"To make {year} the season the commands default to, run "
                f"`/commish season current`."
            )
        await interaction.followup.send(_clip("\n".join(lines)))

    @season.command(
        name="platform", description="Record which platform a season was played on."
    )
    @app_commands.describe(
        platform="Platform name (defaults to the league's current one)",
        year="Season to pin (leave blank to pin every season that names none)",
    )
    @is_commissioner()
    async def season_platform(
        self,
        interaction: discord.Interaction,
        platform: str | None = None,
        year: int | None = None,
    ) -> None:
        """Pin a season's platform, so a later league-wide move leaves it alone.

        The dims key a manager by ``(platform, account id)`` and read the
        platform per season, falling back to the league's. A league that moves
        therefore drags every unpinned season with it: past seasons' accounts
        are re-read under the new platform, minting a second manager for each
        of them. Pinning history first is what stops that — after which
        changing the league's own ``platform`` and reloading (which builds a
        fresh EloSystem, and so a fresh scraper) only affects seasons from the
        move onwards.
        """
        from elo_system.tools.elo_league import LEAGUE_CLASSES

        await interaction.response.defer(ephemeral=True)
        system, league = await self._league(interaction)
        if league is None:
            return
        target = (platform or league.platform or "").strip()
        if not target:
            await interaction.followup.send(
                "This league names no platform, so there is nothing to default to — "
                "pass one explicitly."
            )
            return
        if year is not None and year not in league.seasons:
            await interaction.followup.send(
                f"Season {year} isn't configured. Available: "
                + (", ".join(str(y) for y in sorted(league.seasons)) or "none")
            )
            return
        if year is not None:
            years = [year]
        else:
            years = [y for y in sorted(league.seasons) if league.seasons[y].get("platform") is None]
            if not years:
                await interaction.followup.send(
                    "Every season already names the platform it was played on."
                )
                return

        def _work():
            for season_year in years:
                league.seasons[season_year]["platform"] = target
            league.dump()
            system.write_configs("ratings")

        try:
            await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("season platform failed")
            await interaction.followup.send(f"Couldn't pin the platform: `{exc}`")
            return
        self.bot.leagues.invalidate(interaction.guild_id)

        listed = ", ".join(str(y) for y in years)
        lines = [f"Pinned season(s) **{listed}** to `{target}`."]
        if target not in LEAGUE_CLASSES:
            # Fine for a league's past — the dims only need the name — but it
            # cannot be scraped, so say so rather than let a typo pass quietly.
            lines.append(
                f"⚠️ `{target}` isn't a platform the scraper knows "
                f"({', '.join(sorted(LEAGUE_CLASSES))}), so those seasons can't be "
                "re-scraped. That is expected for a platform the league has left — "
                "but check the spelling."
            )
        lines.append(
            "Their managers stay filed under that platform, so moving the league "
            "elsewhere now only creates accounts for the seasons that follow."
        )
        await interaction.followup.send("\n".join(lines))

    @season.command(
        name="current", description="Set which season this league's commands default to."
    )
    @app_commands.describe(
        year="The season to make current (leave blank for the latest configured)"
    )
    @is_commissioner()
    async def season_current(
        self, interaction: discord.Interaction, year: int | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        system, league = await self._league(interaction)
        if league is None:
            return
        if not league.seasons:
            await interaction.followup.send(
                f"`{system.league}` has no seasons configured — add one with "
                "`/commish season add`."
            )
            return
        reset = year is None
        target = max(league.seasons) if reset else year
        if target not in league.seasons:
            await interaction.followup.send(
                f"Season {target} isn't configured. Available: "
                + ", ".join(str(y) for y in sorted(league.seasons))
            )
            return
        previous = league.current_sports_year

        def _work():
            # current_sports_year is what the run/publish pipelines fall back to;
            # current_season is which season's League object gets loaded. A
            # commissioner means both by "the current season", so both move.
            league.current_sports_year = target
            league.current_season = target
            league.dump()
            system.write_configs("ratings")

        try:
            await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("season current failed")
            await interaction.followup.send(f"Couldn't set the current season: `{exc}`")
            return
        self.bot.leagues.invalidate(interaction.guild_id)
        how = " (the latest configured)" if reset else ""
        was = f" It was {previous}." if previous != target else ""
        await interaction.followup.send(
            f"`{system.league}` now defaults to season **{target}**{how}.{was}\n"
            "`/elo run` and `/commish publish` use it when no season is given."
        )

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

    @db.command(name="managers", description="List managers, with platform names, to link.")
    @app_commands.describe(
        league="Only this league's managers (defaults to all)",
        search="Only managers whose name (real or platform) matches this",
    )
    @is_commissioner()
    async def db_managers(
        self,
        interaction: discord.Interaction,
        league: str | None = None,
        search: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        elo_sql = await self._db_backend(interaction)
        if elo_sql is None:
            return

        # A league filter needs that league's id resolved off its own system.
        league_id = None
        if league is not None:
            try:
                system = await get_or_build(self.bot, interaction.guild_id, league)
            except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
                await interaction.followup.send(f"Couldn't load league `{league}`: `{exc}`")
                return
            if system.elo_sql is None:
                await interaction.followup.send("No SQL backend is configured.")
                return
            elo_sql = system.elo_sql
            league_id = await asyncio.to_thread(elo_sql.set_league_config, system.ratings_config)
            if league_id is None or league_id < 0:
                await interaction.followup.send(f"League `{league}` isn't in the database yet.")
                return

        try:
            rows = await asyncio.to_thread(_manager_rows, elo_sql, league_id)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("db managers failed")
            await interaction.followup.send(f"Couldn't read managers: `{exc}`")
            return
        if search:
            needle = search.lower()
            rows = [
                r for r in rows
                if needle in str(r[1]).lower()
                or any(needle in str(d).lower() for d in r[2].values())
            ]
        if not rows:
            await interaction.followup.send("No matching managers.")
            return
        anon = self.bot.discord_anon
        shown = rows[:25]
        lines = [
            f"`{mid}` — **{pname}** · {_platform_str(plats)} · {_manager_ref(anon, did)}"
            for mid, pname, plats, did in shown
        ]
        footer = f"\n…and {len(rows) - 25} more (narrow with `search`)." if len(rows) > 25 else ""
        await interaction.followup.send(
            _clip("**Managers**\n" + "\n".join(lines) + footer),
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
            updated = _update_column(
                elo_sql, "dim_manager", "discord_id",
                surrogate, "manager_id", manager_id,
            )
            # Read back who that manager is, to confirm the right one was linked.
            return updated, (_manager_rows(elo_sql) if updated else [])

        try:
            updated, rows = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("db linkuser failed")
            await interaction.followup.send(f"Link failed: `{exc}`")
            return
        if not updated:
            await interaction.followup.send(f"No manager with `manager_id` {manager_id}.")
            return
        identity = _manager_identity(rows, manager_id)
        who = f"{identity} (manager `{manager_id}`)" if identity else f"manager `{manager_id}`"
        await interaction.followup.send(
            f"Attached {user.mention} to {who}.",
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
        description="Adopt an existing SQL league for THIS server (do this before linking users).",
    )
    @app_commands.describe(league="League key to convert (defaults to this server's)")
    @is_commissioner()
    async def db_migrate(
        self, interaction: discord.Interaction, league: str | None = None
    ) -> None:
        """Convert an existing SQL league so this server owns it.

        This is the first step: it binds this Discord server to the league's
        ``dim_league`` row (storing a surrogate for the server id) and to the
        bot's per-guild config. It does *not* touch managers — a manager's Elo
        identity is their Fantrax/Sleeper account, which can't be matched to a
        Discord user automatically, so users are linked afterwards, one at a
        time, with ``/commish db linkuser`` / ``adduser``. The reply lists the
        league's roster (with platform names) so you know who to link next.
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
        guild_id = interaction.guild_id

        def _work():
            league_id = elo_sql.set_league_config(system.ratings_config)
            if league_id is None or league_id < 0:
                return None, []
            # Store an opaque surrogate for this server, not the real guild id.
            surrogate = self.bot.discord_anon.surrogate(CATEGORY_SERVER, guild_id)
            _update_column(
                elo_sql, "dim_league", "discord_server_id",
                surrogate, "league_id", league_id,
            )
            return league_id, _manager_rows(elo_sql, league_id)

        try:
            league_id, rows = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the commish
            log.exception("migrate failed")
            await interaction.followup.send(f"Migration failed: `{exc}`")
            return
        if league_id is None:
            await interaction.followup.send(
                f"League `{system.league}` isn't in the database yet — sync/publish it first."
            )
            return

        # Bind this server to the league on the bot side too.
        self.bot.configs.set_guild(guild_id, {GUILD_LEAGUE_KEY: system.league})

        anon = self.bot.discord_anon
        linked = sum(1 for _, _, _, did in rows if anon.real(CATEGORY_MANAGER, did) is not None)
        header = (
            f"Converted **{system.league}** and linked it to this server.\n"
            f"{len(rows)} manager(s), {linked} already linked. Assign the rest with "
            f"`/commish db linkuser` (or `/commish db adduser`):"
        )
        lines = [
            f"`{mid}` — **{pname}** · {_platform_str(plats)} · {_manager_ref(anon, did)}"
            for mid, pname, plats, did in rows
        ]
        body = "\n".join(lines) if lines else "(no managers found for this league)"
        await interaction.followup.send(
            _clip(f"{header}\n{body}"),
            allowed_mentions=discord.AllowedMentions.none(),
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
    """Render a stored discord_id: a mention when we know it, else its status."""
    if _is_missing(stored):
        return "— (unlinked)"
    real = anon.real(CATEGORY_MANAGER, stored)
    if real is not None:
        return f"<@{real}>"
    text = str(stored).strip()
    if text.isdigit():
        # Numeric but not one of ours — a real id a pre-surrogate build left, or
        # a foreign value. Shown, not mentioned, so a bad row is visible.
        return f"`{text}` (opaque)"
    return "— (unlinked)"  # an alphanumeric id_generator seed = never linked


def _platform_str(platforms: dict) -> str:
    """Render a manager's platform display names, e.g. 'fantrax: natedog'."""
    if not platforms:
        return "—"
    return ", ".join(f"{p}: {name}" for p, name in platforms.items())


def _manager_identity(rows, manager_id) -> str | None:
    """A manager's ``**name** · platform: display`` label from _manager_rows, or None."""
    for mid, name, platforms, _ in rows:
        if mid == manager_id:
            platform = _platform_str(platforms)
            return f"**{name}**" + (f" · {platform}" if platform != "—" else "")
    return None


def _manager_rows(elo_sql, league_id=None) -> list:
    """Managers as ``(manager_id, player_name, {platform: display_name}, discord_id)``.

    Reads through EloSQL's dim cache (deanonymized, so real names show). With a
    ``league_id`` it keeps only managers who own a team in that league's seasons.
    Values are the raw stored discord_id (a surrogate, a seed, or None).
    """
    elo_sql.pull_dims("manager", overwrite=True)
    managers = elo_sql.dim_tables["dim_manager"].reset_index()

    # Platform display names (its own dim; composite key, so already flat).
    platforms: dict[int, dict] = {}
    try:
        elo_sql.pull_dims("manager_platform", overwrite=True)
        mp = elo_sql.dim_tables["dim_manager_platform"]
        if "manager_id" not in mp.columns:
            mp = mp.reset_index()
        for mid, platform, display in zip(mp["manager_id"], mp["platform"], mp["display_name"]):
            if _is_missing(display):
                continue
            platforms.setdefault(int(mid), {})[str(platform)] = display
    except Exception:  # noqa: BLE001 - platform names are a nicety, not essential
        log.warning("couldn't load platform display names", exc_info=True)

    # Optional league scope: managers with a team in one of the league's seasons.
    keep = None
    if league_id is not None:
        elo_sql.pull_dims("online_league", overwrite=True)
        elo_sql.pull_dims("team", overwrite=True)
        online = elo_sql.dim_tables["dim_online_league"].reset_index()
        team = elo_sql.dim_tables["dim_team"].reset_index()
        online_ids = set(online.loc[online["league_id"] == league_id, "online_league_id"])
        keep = {int(m) for m in team.loc[team["online_league_id"].isin(online_ids), "manager_id"]}

    rows = []
    for mid, name, discord_id in zip(
        managers["manager_id"], managers["player_name"], managers["discord_id"]
    ):
        mid = int(mid)
        if keep is not None and mid not in keep:
            continue
        rows.append((mid, name, platforms.get(mid, {}), discord_id))
    return rows


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
