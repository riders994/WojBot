"""Configuration loading for WojBot.

Settings come from environment variables (optionally seeded from a local ``.env``
file). Unlike v1 — which read the Discord token at import time and blew up with a
bare ``KeyError`` if it was unset — nothing here touches the environment until
:meth:`Settings.load` is called, and a missing token raises a clear error.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

from .errors import ConfigError

# Project root: .../WojBot (two parents up from this file: core/ -> wojbot/ -> root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# The discrete connection fields, for when a whole URI is inconvenient to write.
SQL_FIELDS = ("SQL_HOST", "SQL_PORT", "SQL_USER", "SQL_PASSWORD", "SQL_DBNAME")
# What it takes to build a URI at all. Port has a default and a password may
# legitimately be empty (peer or trust auth), so neither is required.
SQL_REQUIRED = ("SQL_HOST", "SQL_USER", "SQL_DBNAME")
DEFAULT_SQL_PORT = "5432"


def resolve_sql_uri(env: Mapping[str, str]) -> str | None:
    """The Postgres URI to connect with, or None to run without a database.

    ``SQL_CONN_URI`` wins if it is set: a URI somebody pasted is the more
    specific instruction, and it can express things the fields can't (an
    ``sslmode``, a connection timeout, a socket path). Otherwise the discrete
    ``SQL_*`` fields are assembled into one, which is the easier form to write
    by hand and the only one that copes with a password containing ``@`` or
    ``/`` without the author having to percent-encode it themselves.

    Raises:
        ConfigError: if some fields are set but not enough to connect. Silently
            falling back to "no database" there would turn a typo into a bot
            that starts fine and has simply lost half its commands.
    """
    uri = (env.get("SQL_CONN_URI") or "").strip()
    if uri:
        return uri

    values = {key: (env.get(key) or "").strip() for key in SQL_FIELDS}
    if not any(values.values()):
        return None  # nothing configured at all: database features stay off

    missing = [key for key in SQL_REQUIRED if not values[key]]
    if missing:
        raise ConfigError(
            f"Incomplete database settings: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not set. Either fill in "
            f"{', '.join(SQL_REQUIRED)} (SQL_PORT and SQL_PASSWORD are optional), "
            "set SQL_CONN_URI instead, or clear them all to run without a database."
        )

    host = values["SQL_HOST"]
    # A bare IPv6 literal has to be bracketed or the port reads as part of it.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    # safe="" so that a password's own : / @ ? # are encoded rather than
    # silently splitting the URI somewhere else.
    credentials = quote(values["SQL_USER"], safe="")
    if values["SQL_PASSWORD"]:
        credentials += f":{quote(values['SQL_PASSWORD'], safe='')}"
    port = values["SQL_PORT"] or DEFAULT_SQL_PORT
    database = quote(values["SQL_DBNAME"], safe="")
    return f"postgresql://{credentials}@{host}:{port}/{database}"


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings for the bot (secrets & environment).

    Distinct from the user-facing bot/guild/league config in
    :class:`wojbot.core.config_store.ConfigStore` — this holds the token,
    connection strings, and other deploy-time environment values.
    """

    token: str
    guild_id: int | None
    log_level: str
    # Postgres DSN for the SQL cog; None disables DB features. Resolved by
    # resolve_sql_uri from either SQL_CONN_URI or the discrete SQL_* fields, so
    # everything downstream only ever sees the one assembled URI.
    sql_conn_uri: str | None

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from the environment (and an optional ``.env`` file).

        Raises:
            ConfigError: if ``DISCORD_TOKEN`` is not set, ``DISCORD_GUILD_ID``
                isn't an integer, or the database settings are half-filled.
        """
        # Loads .env if present; a missing file is fine (no error).
        load_dotenv()

        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            raise ConfigError(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in, "
                "or export DISCORD_TOKEN in the environment."
            )

        guild_raw = os.environ.get("DISCORD_GUILD_ID")
        try:
            guild_id = int(guild_raw) if guild_raw else None
        except ValueError as exc:
            raise ConfigError(
                f"DISCORD_GUILD_ID must be an integer, got {guild_raw!r}."
            ) from exc

        log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        sql_conn_uri = resolve_sql_uri(os.environ)

        return cls(
            token=token,
            guild_id=guild_id,
            log_level=log_level,
            sql_conn_uri=sql_conn_uri,
        )
