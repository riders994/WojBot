"""Configuration loading for WojBot.

Settings come from environment variables (optionally seeded from a local ``.env``
file). Unlike v1 — which read the Discord token at import time and blew up with a
bare ``KeyError`` if it was unset — nothing here touches the environment until
:meth:`Settings.load` is called, and a missing token raises a clear error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError

# Project root: .../WojBot (two parents up from this file: core/ -> wojbot/ -> root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


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
    sql_conn_uri: str | None  # Postgres DSN for the SQL cog; None disables DB features

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from the environment (and an optional ``.env`` file).

        Raises:
            ConfigError: if ``DISCORD_TOKEN`` is not set.
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
        sql_conn_uri = os.environ.get("SQL_CONN_URI") or None

        return cls(
            token=token,
            guild_id=guild_id,
            log_level=log_level,
            sql_conn_uri=sql_conn_uri,
        )
