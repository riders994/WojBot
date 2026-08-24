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

Stored server files carry a ``_schema`` version so a renamed setting can be
migrated on load rather than reinterpreted in place; see
:data:`SERVER_SCHEMA_VERSION`.
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

# Bumped whenever a stored server key changes meaning. Files written before the
# marker existed have no ``_schema`` at all and are read as version 1.
SERVER_SCHEMA_VERSION = 2
_SCHEMA_KEY = "_schema"

# v1 -> v2, the four-tier permissions rework. Old key -> new key.
#
# This is a **simultaneous** remap, and it has to be: `admin_roles` is both a
# source and a destination. Applied one pair at a time, in the wrong order, the
# old lower-bar admin list would land in the new Admin tier and quietly hand
# `/commish publish` and all of `/commish db` to every `mods` in every server.
#
# It also cannot be inferred from which keys are present, which is what the
# schema marker is for: a v1 file that only ever set `admin_roles` and a v2 file
# that sets `admin_roles` look identical, and mean opposite things.
_V2_TIER_RENAMES = {
    "commissioner_roles": "admin_roles",
    "admin_roles": "verified_roles",
}


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


def _migrate_server(stored: dict) -> bool:
    """Bring one stored server file up to :data:`SERVER_SCHEMA_VERSION`.

    Mutates ``stored`` in place and returns whether anything changed, so the
    caller knows whether to persist. A file already at the current version is
    left alone, which makes this safe to run on every load.
    """
    version = stored.get(_SCHEMA_KEY, 1)
    if version >= SERVER_SCHEMA_VERSION:
        return False

    if version < 2:
        # Snapshot first, then clear, then write: reading and writing in one
        # pass would let the `admin_roles` result be re-read as a source.
        moved = {
            new: stored[old]
            for old, new in _V2_TIER_RENAMES.items()
            if old in stored
        }
        for old in _V2_TIER_RENAMES:
            stored.pop(old, None)
        stored.update(moved)
        if moved:
            log.info(
                "Migrated permission tiers to v2: %s",
                ", ".join(f"{old} -> {new}" for old, new in _V2_TIER_RENAMES.items()),
            )

    stored[_SCHEMA_KEY] = SERVER_SCHEMA_VERSION
    return True


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
        self._migrate_guilds()

    def _migrate_guilds(self) -> None:
        """Migrate every stored server file, persisting the ones that moved.

        Writing back at load is deliberate rather than lazy: a migration that
        lived only in memory would be redone on every start, and would be
        silently undone the first time an unmigrated key was written by an
        older build.
        """
        for guild_id, stored in self._guilds.items():
            if _migrate_server(stored):
                log.info("Migrated stored config for guild %s", guild_id)
                _write_yaml(self.base_dir / _SERVERS_DIR / f"{guild_id}.yaml", stored)

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
        merged = {**DEFAULT_SERVER_CONFIG, **inherited, **stored}
        # Bookkeeping, not a setting -- callers iterate this dict to show a
        # server what it has configured, and the schema version is not that.
        merged.pop(_SCHEMA_KEY, None)
        return merged

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
        # Stamp the version on anything we write. Without this, a file this
        # build creates would carry no marker, be read as v1 on the next start,
        # and get "migrated" -- moving its admin_roles down into verified_roles
        # and demoting the server's Admins on a restart nobody connected to it.
        stored[_SCHEMA_KEY] = SERVER_SCHEMA_VERSION
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
