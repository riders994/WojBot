"""Async wrapper around rv_pytools' SQL Manager.

The bot uses a single shared Postgres warehouse (all leagues live in one DB and
queries filter by ``league_id``). rv_pytools' :class:`Manager` gives us a
named-connection pool and a file-based named-query registry (``sql/queries/*.sql``);
this wrapper adapts its *synchronous* psycopg2 API for discord.py's event loop by
running every blocking call in a thread and serializing access behind a lock
(psycopg2 connections are not safe for concurrent use).

The connection URI comes from the ``SQL_CONN_URI`` environment variable (see
:class:`wojbot.core.config.Settings`).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from rv_pytools.sqltools import Manager

from .config import PROJECT_ROOT
from .errors import ConfigError

log = logging.getLogger(__name__)

SQL_DIR = PROJECT_ROOT / "sql"
DEFAULT_CONNECTION = "wojbot"


class SqlService:
    """Async facade over a rv_pytools :class:`Manager`.

    Building the service (loading the query registry) does not require a live
    database; :meth:`connect` opens the connection separately so the bot can
    start — and expose ``/sql reconnect`` — even when the DB is down.
    """

    def __init__(self, manager: Manager, default_connection: str = DEFAULT_CONNECTION) -> None:
        self._manager = manager
        self._default = default_connection
        self._lock = asyncio.Lock()

    @classmethod
    def create(cls, sql_dir: Path = SQL_DIR, name: str = DEFAULT_CONNECTION) -> "SqlService":
        """Build the Manager and load the named-query registry (no DB needed)."""
        manager = Manager(sql_dir=sql_dir, log_path=sql_dir / "manager.log.json")
        manager.run_new_files(subdir="queries")  # register any new sql/queries/*.sql
        return cls(manager, name)

    async def connect(self, conn_uri: str) -> None:
        """Open (or replace) the default connection using ``conn_uri``."""
        if not conn_uri:
            raise ConfigError("No SQL connection URI provided (set SQL_CONN_URI).")
        async with self._lock:
            await asyncio.to_thread(
                self._manager.connect, self._default, {"dsn": conn_uri}
            )

    async def execute(self, name: str | list[str], connector: str | None = None, **kwargs) -> list:
        """Run a registered query (or list of them) and return the fetched rows.

        ``kwargs`` fill ``{}``-style placeholders in the query text (rv_pytools'
        contract) — pass only validated/typed values (e.g. an int ``league_id``).
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._manager.execute_query, name, connector or self._default, **kwargs
            )

    async def status(self) -> dict[str, bool]:
        """Return ``{connection_name: is_open}`` for every held connection."""
        async with self._lock:
            return await asyncio.to_thread(self._manager.connection_status)

    @property
    def queries(self) -> dict[str, str]:
        """The registered named queries (name -> SQL text)."""
        return self._manager.queries

    def close(self) -> None:
        for conn in self._manager.connections.values():
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup on shutdown
                log.warning("Error closing a SQL connection", exc_info=True)
