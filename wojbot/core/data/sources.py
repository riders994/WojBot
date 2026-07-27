"""Data sources for authoritative league data.

A :class:`DataSource` abstracts *where* league data comes from so the rest of the
bot (see :class:`wojbot.core.data.cache.LeagueCache`) doesn't care. Two
implementations are planned:

* :class:`PostgresSource` — the remote Postgres warehouse (via ``asyncpg``),
  authoritative for the dim/fact tables the Yahoo pipeline populates.
* :class:`LocalSource` — CSV/JSON snapshots under ``resources/`` for offline use
  and tests.

Only the interface and skeletons exist today; the query logic lands with the
commish/elo cogs.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from .models import LeagueData

log = logging.getLogger(__name__)


class DataSource(ABC):
    """Async source of league/domain data."""

    @abstractmethod
    async def fetch_league_for_guild(self, guild_id: int) -> LeagueData | None:
        """Return the league bound to ``guild_id``, or ``None`` if there isn't one."""

    async def close(self) -> None:
        """Release any resources (connection pools, file handles). No-op by default."""


class PostgresSource(DataSource):
    """Remote Postgres source (via ``asyncpg``) — not implemented yet.

    Arrives with the commish/elo cogs. Named SQL queries will live under
    ``wojbot/core/data/queries/*.sql`` and connect using
    :attr:`wojbot.core.config.Settings.database_url`. Install with the ``postgres``
    extra (``pip install -e '.[postgres]'``).
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None

    async def connect(self) -> None:
        raise NotImplementedError(
            "PostgresSource is pending: add asyncpg, a connection pool, and the "
            "named queries when the commish/elo cogs are ported."
        )

    async def fetch_league_for_guild(self, guild_id: int) -> LeagueData | None:
        raise NotImplementedError("PostgresSource is pending.")


class LocalSource(DataSource):
    """Reads league snapshots from local files under ``resources/``.

    A minimal, dependency-free source useful offline and in tests. Returns
    ``None`` until snapshot loading is implemented alongside the league cogs.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    async def fetch_league_for_guild(self, guild_id: int) -> LeagueData | None:
        # No local guild->league mapping is stored yet; wired up when the
        # commish cog (which binds a league to a guild) is ported.
        return None
