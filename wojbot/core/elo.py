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

from .config import PROJECT_ROOT

ELO_CONFIG_DIR = PROJECT_ROOT / "resources" / "configs"
ELO_SYS_CONFIG = ELO_CONFIG_DIR / "sys_config.yml"

# Per-guild config key: which configured league this server drives.
GUILD_LEAGUE_KEY = "elo_league"


def runtime_key(league: str | None) -> str:
    """Runtime registry key for a league's EloSystem."""
    return f"elo:{league or '<default>'}"


def configured_league(bot, guild_id: int | None) -> str | None:
    """The league key bound to this guild, if any."""
    if guild_id is None:
        return None
    return bot.configs.get_guild(guild_id).get(GUILD_LEAGUE_KEY)


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
