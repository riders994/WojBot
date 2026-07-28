"""In-memory caching of league data, plus a registry for live objects.

This is the "data in the bot, optimized for usage" layer: instead of hitting
Postgres on every command, a guild's :class:`LeagueData` is loaded once from a
:class:`~wojbot.core.data.sources.DataSource` and reused until it expires or is
explicitly invalidated (e.g. after a commissioner ``dump``/refresh).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .models import LeagueData
from .sources import DataSource

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 15 * 60


class LeagueCache:
    """Lazily loads and caches league data per guild, with a TTL."""

    def __init__(self, source: DataSource, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self._source = source
        self._ttl = ttl
        # guild_id -> (loaded_at_monotonic, data)
        self._cache: dict[int, tuple[float, LeagueData | None]] = {}

    async def get_for_guild(self, guild_id: int) -> LeagueData | None:
        now = time.monotonic()
        cached = self._cache.get(guild_id)
        if cached is not None and (now - cached[0]) < self._ttl:
            return cached[1]

        data = await self._source.fetch_league_for_guild(guild_id)
        self._cache[guild_id] = (now, data)
        return data

    def invalidate(self, guild_id: int | None = None) -> None:
        """Drop one guild's cached data, or all of it when ``guild_id`` is None."""
        if guild_id is None:
            self._cache.clear()
        else:
            self._cache.pop(guild_id, None)

    async def close(self) -> None:
        await self._source.close()


class Runtime:
    """Registry for long-lived, non-persisted objects.

    Holds things that are expensive to build and reused across commands — the
    Yahoo scraper session, an Elo engine — but never serialized. Rebuilt on
    demand. Replaces v1's ``recycling``/``compost``/``garbage`` memory tiers with
    one explicit registry.
    """

    def __init__(self) -> None:
        self._objects: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        return self._objects.get(key)

    def set(self, key: str, value: Any) -> None:
        self._objects[key] = value

    def pop(self, key: str) -> Any | None:
        return self._objects.pop(key, None)

    def clear(self) -> None:
        self._objects.clear()
