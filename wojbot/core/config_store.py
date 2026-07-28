"""Persistent bot/guild/league configuration, backed by flat YAML files.

Layout under ``resources/configs/``::

    bot_config.yaml        # global bot settings
    servers/<guild_id>.yaml
    leagues/<league_id>.yaml

All files are loaded into memory once at startup, so reads (which happen on
every message for things like the dad-joke toggle) are pure dict lookups.
Writes update the in-memory copy and persist atomically. Only *overrides* are
stored; :func:`get_*` merges them over the defaults in
:mod:`wojbot.core.defaults`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from .config import PROJECT_ROOT
from .defaults import (
    DEFAULT_BOT_CONFIG,
    DEFAULT_LEAGUE_CONFIG,
    DEFAULT_SERVER_CONFIG,
    INHERITED_FROM_BOT,
)

log = logging.getLogger(__name__)

CONFIG_DIR = PROJECT_ROOT / "resources" / "configs"
_BOT_FILE = "bot_config.yaml"
_SERVERS_DIR = "servers"
_LEAGUES_DIR = "leagues"


def _read_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _write_yaml(path: Path, data: dict) -> None:
    """Atomically write ``data`` as YAML (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=True)
    os.replace(tmp, path)


def _read_scope_dir(directory: Path) -> dict[int, dict]:
    """Load every ``<int>.yaml`` in a scope directory, keyed by that int id."""
    result: dict[int, dict] = {}
    if not directory.is_dir():
        return result
    for path in directory.glob("*.yaml"):
        try:
            key = int(path.stem)
        except ValueError:
            log.warning("Skipping config file with non-numeric name: %s", path)
            continue
        result[key] = _read_yaml(path)
    return result


class ConfigStore:
    """In-memory, write-through store for bot/guild/league settings."""

    def __init__(self, base_dir: Path = CONFIG_DIR) -> None:
        self.base_dir = base_dir
        self._bot: dict = {}
        self._guilds: dict[int, dict] = {}
        self._leagues: dict[int, dict] = {}

    @classmethod
    def load(cls, base_dir: Path = CONFIG_DIR) -> "ConfigStore":
        store = cls(base_dir)
        store.reload()
        return store

    def reload(self) -> None:
        self._bot = _read_yaml(self.base_dir / _BOT_FILE)
        self._guilds = _read_scope_dir(self.base_dir / _SERVERS_DIR)
        self._leagues = _read_scope_dir(self.base_dir / _LEAGUES_DIR)

    # --- reads (defaults merged; returns a fresh dict callers may not mutate) ---

    def get_bot(self) -> dict:
        return {**DEFAULT_BOT_CONFIG, **self._bot}

    def get_guild(self, guild_id: int) -> dict:
        """This server's settings: code defaults, then bot-wide, then its own.

        The middle layer is what makes a bot-wide setting a *default* rather
        than an override — a server that has stored its own value keeps it, so
        moving the global never silently changes a server that already chose.
        """
        stored = self._guilds.get(guild_id, {})
        inherited = {
            key: value for key, value in self.get_bot().items()
            if key in INHERITED_FROM_BOT
        }
        return {**DEFAULT_SERVER_CONFIG, **inherited, **stored}

    def guild_has_own(self, guild_id: int, key: str) -> bool:
        """Whether this server set ``key`` itself, as against inheriting it."""
        return key in self._guilds.get(guild_id, {})

    def get_league(self, league_id: int) -> dict:
        return {**DEFAULT_LEAGUE_CONFIG, **self._leagues.get(league_id, {})}

    # --- writes (merge patch into stored overrides, persist, return merged view) ---

    def set_bot(self, patch: dict) -> dict:
        self._bot.update(patch)
        _write_yaml(self.base_dir / _BOT_FILE, self._bot)
        return self.get_bot()

    def set_guild(self, guild_id: int, patch: dict) -> dict:
        stored = self._guilds.setdefault(guild_id, {})
        stored.update(patch)
        _write_yaml(self.base_dir / _SERVERS_DIR / f"{guild_id}.yaml", stored)
        return self.get_guild(guild_id)

    def clear_guild(self, guild_id: int, *keys: str) -> dict:
        """Drop this server's own value for ``keys``, so it inherits again.

        The counterpart to setting one: without it a server that ever chose is
        stuck with its choice for good, because storing a value is what detaches
        it from the bot-wide default.
        """
        stored = self._guilds.get(guild_id)
        if not stored or not any(key in stored for key in keys):
            return self.get_guild(guild_id)
        for key in keys:
            stored.pop(key, None)
        _write_yaml(self.base_dir / _SERVERS_DIR / f"{guild_id}.yaml", stored)
        return self.get_guild(guild_id)

    def set_league(self, league_id: int, patch: dict) -> dict:
        stored = self._leagues.setdefault(league_id, {})
        stored.update(patch)
        _write_yaml(self.base_dir / _LEAGUES_DIR / f"{league_id}.yaml", stored)
        return self.get_league(league_id)
