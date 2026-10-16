"""Shared Elo-system helpers: one EloSystem per league, cached on the bot.

Both the commish cog (which runs/publishes ratings) and the annuhlitucks cog
(which displays them) drive the same :class:`elo_system.EloSystem`. There must be
exactly one instance per league so they share loaded frames and the DB
connection, so construction, caching, and league resolution live here rather
than in either cog.

Config lives in ``resources/configs/`` (a shared ``sys_config.yml`` names the
leagues). Each guild is bound to a league key via per-guild config
(:data:`GUILD_LEAGUE_KEY`, set by ``/commish load``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import yaml

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

ELO_CONFIG_DIR = PROJECT_ROOT / "resources" / "configs"
ELO_SYS_CONFIG = ELO_CONFIG_DIR / "sys_config.yml"

# Per-guild config key: which configured league this server drives.
GUILD_LEAGUE_KEY = "elo_league"

# Per-guild config key: which ratings a dynasty league's Elo reports show.
# A dynasty league keeps two -- each season on its own, and the all-time
# timeline -- and a server picks which it hears about. A league that isn't a
# dynasty only has the one, so this changes nothing for it.
ELO_REPORT_KEY = "elo_report"
REPORT_SEASONAL = "seasonal"
REPORT_DYNASTY = "dynasty"
REPORT_BOTH = "both"
REPORT_CHOICES = (REPORT_SEASONAL, REPORT_DYNASTY, REPORT_BOTH)


def report_scopes(setting: str | None, is_dynasty: bool) -> list[str]:
    """Which ratings a report shows: ``seasonal``, ``dynasty``, or both, in order."""
    if not is_dynasty:
        return [REPORT_SEASONAL]
    if setting == REPORT_DYNASTY:
        return [REPORT_DYNASTY]
    if setting == REPORT_BOTH:
        return [REPORT_SEASONAL, REPORT_DYNASTY]
    # Unset or unrecognised: seasonal, the server default.
    return [REPORT_SEASONAL]


def runtime_key(league: str | None) -> str:
    """Runtime registry key for a league's EloSystem."""
    return f"elo:{league or '<default>'}"


def configured_league(bot, guild_id: int | None) -> str | None:
    """The league key bound to this guild, if any."""
    if guild_id is None:
        return None
    return bot.configs.get_guild(guild_id).get(GUILD_LEAGUE_KEY)


def ratings_config(bot, guild_id: int | None) -> dict | None:
    """This guild's league config as YAML, **without building an EloSystem**.

    The cheap read. Everything else here constructs an :class:`EloSystem`, which
    is right when the caller is about to rate a season and wrong when it only
    wants to know what sport the league plays or which platform a season ran on
    -- questions asked while somebody waits on a slash command.

    Prefers the loaded system's config when there is one, so a commish who has
    just edited a season sees the edit rather than the file behind it. None when
    the server is bound to no league, or the files can't be read.
    """
    if guild_id is None:
        return None

    system = cached_system(bot, guild_id)
    if system is not None and getattr(system, "ratings_config", None):
        return system.ratings_config

    league = configured_league(bot, guild_id)
    if league is None:
        return None
    try:
        sys_config = yaml.safe_load(ELO_SYS_CONFIG.read_text()) or {}
        filename = (sys_config.get("ratings_configs") or {}).get(league)
        if not filename:
            return None
        return yaml.safe_load((ELO_CONFIG_DIR / filename).read_text()) or {}
    except (OSError, yaml.YAMLError):
        # Callers use this to answer "which sport?" and "which platform?", and
        # both have a sane fallback. An unreadable config file is not a reason
        # to refuse to answer at all.
        log.warning("Couldn't read the ratings config for %r", league, exc_info=True)
        return None


def season_platform(ratings: dict, year: int | None = None) -> str | None:
    """Which platform a season ran on -- the season's own, then the league's.

    Same rule the Elo package's dims follow: a season pinned by
    ``/commish season platform`` keeps its platform when the league moves, and
    an unpinned one follows the league. ``year`` defaults to the current season.
    """
    if not ratings:
        return None
    if year is None:
        year = ratings.get("current_sports_year")
    season = (ratings.get("seasons") or {}).get(year) or {}
    return season.get("platform") or ratings.get("platform")


async def build_system(bot, league: str | None):
    """Construct an EloSystem for ``league`` and load its configs (off-thread).

    ``league=None`` lets EloSystem fall back to ``default_league`` (or raise if
    ambiguous). When the bot has a live DB connection, the Elo SQL backend is
    rebuilt to reuse it (same warehouse) instead of opening its own.
    """
    from elo_system.elo_system import EloSystem

    sql = getattr(bot, "sql", None)
    connector = sql.connection if sql is not None else None

    def _build():
        system = EloSystem(str(ELO_SYS_CONFIG), league)
        system.load_configs()
        if connector is not None and system.sql_config:
            system.set_elo_sql(system.sql_config, connector)
            system.set_reader(system.reader_key)
            system.set_writer(system.writer_key)
        return system

    return await asyncio.to_thread(_build)


def cached_system(bot, guild_id: int | None):
    """The EloSystem already loaded for this guild's league, or None."""
    return bot.runtime.get(runtime_key(configured_league(bot, guild_id)))


def season_ids(ratings_config: dict) -> set[tuple[str, str]]:
    """The (platform, platform league id) pairs a league's seasons are known by.

    A season names its own platform when pinned, and otherwise plays on the
    league's -- the same fallback the SQL dims use.
    """
    platform = ratings_config.get("platform")
    return {
        (season.get("platform") or platform, str(season["league_id"]))
        for season in (ratings_config.get("seasons") or {}).values()
        if season.get("league_id") is not None
    }


def matching_league_keys(pairs, ratings_configs: dict[str, dict]) -> list[str]:
    """Configured league keys whose seasons share a platform id with ``pairs``."""
    wanted = {(platform, str(league_id)) for platform, league_id in pairs}
    return sorted(
        key for key, config in ratings_configs.items() if season_ids(config) & wanted
    )


def read_ratings_configs() -> dict[str, dict]:
    """Every configured league's ratings config, keyed by league key."""
    sys_config = yaml.safe_load(ELO_SYS_CONFIG.read_text()) or {}
    configs = {}
    for key, filename in (sys_config.get("ratings_configs") or {}).items():
        path = ELO_CONFIG_DIR / filename
        if path.exists():
            configs[key] = yaml.safe_load(path.read_text()) or {}
    return configs


@dataclass
class DatabaseLink:
    """The league the database links a server to, and the Elo league that is.

    ``key`` is None when the linked league matches no configured Elo league --
    one that has never been given a ratings config on this host.
    """

    league_id: int
    name: str
    key: str | None


async def database_league(bot, guild_id: int | None) -> DatabaseLink | None:
    """The Elo league behind this server's ``dim_league`` link, if it has one.

    This is the link rumors read, set by ``/commish db migrate`` or ``link``.
    The per-guild ``elo_league`` key is a second, separate copy of the same
    fact, and the one to trust when they disagree is this one: it is what
    every other league-scoped command in the bot already goes by.

    None when there is no database or the server isn't linked to a league.
    """
    if guild_id is None or getattr(bot, "sql", None) is None:
        return None
    # Imported here: rumor is the module that owns the server -> league lookup,
    # and nothing in it needs this one.
    from .rumor import league_for_guild

    league = await league_for_guild(bot, guild_id)
    if league is None:
        return None
    rows = await bot.sql.run("read_league_platform_ids", {"league_id": league.id})
    configs = await asyncio.to_thread(read_ratings_configs)
    keys = matching_league_keys(rows, configs)
    # Two configs sharing a season id would be a misconfiguration; refusing to
    # pick between them is better than picking the wrong one quietly.
    return DatabaseLink(league.id, league.name, keys[0] if len(keys) == 1 else None)


@dataclass
class LoadPlan:
    """Which league ``/commish load`` should load, or why it shouldn't.

    ``key`` None with no ``refusal`` means the system default -- whose name
    is only known once it is built, so the caller words that note.
    """

    key: str | None = None
    refusal: str | None = None
    notes: list[str] | None = None
    uses_default: bool = False


def plan_load(
    requested: str | None, configured: str | None, link: DatabaseLink | None
) -> LoadPlan:
    """Decide what ``/commish load`` loads, given the server's two league links.

    The database link wins, because rumors already go by it: a server whose
    Elo binding disagrees is running ratings for one league and rumors for
    another. So with no league named, the linked one is loaded (and a wrong
    binding corrected); a league named against the link is refused, since
    moving a server between leagues is ``/commish db migrate``'s job. The
    system default is only ever the answer for a server linked to nothing.
    """
    if link is not None and link.key is not None:
        if requested is not None and requested != link.key:
            return LoadPlan(
                refusal=(
                    f"This server is linked to **{link.name}** (`{link.key}`) in the "
                    f"database, and rumors here go by that. Loading `{requested}` would "
                    "rate a different league from the one the server reports on.\n"
                    f"To move the server to `{requested}`, run "
                    f"`/commish db migrate league: {requested}`."
                )
            )
        notes = []
        if configured is not None and configured != link.key:
            notes.append(
                f"This server's Elo was bound to `{configured}`, not the league it's "
                f"linked to — rebound to `{link.key}`."
            )
        return LoadPlan(key=link.key, notes=notes)

    chosen = requested or configured
    notes = []
    if link is not None:
        # Linked, but to a league no ratings config here describes.
        if chosen is None:
            return LoadPlan(
                refusal=(
                    f"This server is linked to **{link.name}** in the database, but no "
                    "configured Elo league has its seasons, so there's nothing to load "
                    "for it. Name one with `league:` if you mean a different league."
                )
            )
        notes.append(
            f"This server is linked to **{link.name}** in the database, which no "
            f"configured Elo league matches — loaded `{chosen}` as asked."
        )
        return LoadPlan(key=chosen, notes=notes)
    return LoadPlan(key=chosen, notes=notes, uses_default=chosen is None)


async def get_or_build(bot, guild_id: int | None, league: str | None = None):
    """Return this guild's EloSystem, building and caching one if needed.

    ``league`` overrides the guild binding. The instance is cached under its
    resolved league key so both cogs reuse it.
    """
    requested = league or configured_league(bot, guild_id)
    if requested is not None:
        existing = bot.runtime.get(runtime_key(requested))
        if existing is not None:
            return existing
    system = await build_system(bot, requested)
    bot.runtime.set(runtime_key(system.league), system)
    return system
