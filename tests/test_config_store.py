"""Tests for :mod:`wojbot.core.config_store`, and mostly for its migration.

The v1 -> v2 rework renamed the permission tiers, and ``admin_roles`` survived
the rename with a *different meaning*: it was the lower of two tiers and is now
the higher of two. So the migration is the one piece of this change that can
fail silently and dangerously — get it wrong and every server's ``mods`` quietly
acquire `/commish publish` and all of `/commish db` on the next restart.

The cases below pin the three ways that could happen:

* the remap running **sequentially** rather than as a snapshot, which would feed
  the new ``admin_roles`` back in as a source,
* the migration being inferred from **which keys are present**, which cannot
  distinguish a v1 file that only set ``admin_roles`` from a v2 file that did,
* a file written by this build carrying **no marker**, so the next start reads
  it as v1 and migrates something already migrated.
"""

from __future__ import annotations

import pytest
import yaml

from wojbot.core.checks import ADMIN_ROLES_KEY, RESTRICTED_ROLES_KEY, VERIFIED_ROLES_KEY
from wojbot.core.config_store import SERVER_SCHEMA_VERSION, ConfigStore

GUILD = 111


@pytest.fixture
def store_dir(tmp_path):
    (tmp_path / "servers").mkdir()
    return tmp_path


def write_server(base, guild_id: int, data: dict) -> None:
    (base / "servers" / f"{guild_id}.yaml").write_text(yaml.safe_dump(data))


def read_server(base, guild_id: int) -> dict:
    return yaml.safe_load((base / "servers" / f"{guild_id}.yaml").read_text())


# --- the v1 -> v2 tier rename ---------------------------------------------


def test_both_old_tiers_move_down_one_place(store_dir):
    """The full old shape: commissioner becomes Admin, admin becomes Verified.

    If this ran sequentially instead of as a snapshot, ``verified_roles`` would
    come out holding ``["Commish"]`` — the value ``admin_roles`` had *after*
    the first step rather than before it.
    """
    write_server(store_dir, GUILD, {
        "commissioner_roles": ["Commish"],
        "admin_roles": ["mods", "Champion", "Commish"],
        "timezone": "America/New_York",
    })
    config = ConfigStore.load(store_dir).get_guild(GUILD)

    assert config[ADMIN_ROLES_KEY] == ["Commish"]
    assert config[VERIFIED_ROLES_KEY] == ["mods", "Champion", "Commish"]
    assert "commissioner_roles" not in config
    # Untouched settings ride through unharmed.
    assert config["timezone"] == "America/New_York"


def test_a_v1_file_that_only_set_admin_roles_is_demoted_not_promoted(store_dir):
    """The case the schema marker exists for.

    A v1 ``admin_roles`` was the *lower* tier, so its contents belong in
    Verified. Reading it as the new Admin tier is the silent escalation this
    whole mechanism is built to prevent, and nothing in the file itself says
    which of the two it is.
    """
    write_server(store_dir, GUILD, {"admin_roles": ["mods"]})
    config = ConfigStore.load(store_dir).get_guild(GUILD)

    assert config[VERIFIED_ROLES_KEY] == ["mods"]
    assert config[ADMIN_ROLES_KEY] == ["Commish"]  # back to the default
    assert "mods" not in config[ADMIN_ROLES_KEY]


def test_the_migration_is_written_back_to_disk(store_dir):
    write_server(store_dir, GUILD, {"commissioner_roles": ["Commish"]})
    ConfigStore.load(store_dir)

    stored = read_server(store_dir, GUILD)
    assert stored["_schema"] == SERVER_SCHEMA_VERSION
    assert stored[ADMIN_ROLES_KEY] == ["Commish"]
    assert "commissioner_roles" not in stored


def test_migrating_twice_changes_nothing(store_dir):
    write_server(store_dir, GUILD, {
        "commissioner_roles": ["Commish"], "admin_roles": ["mods"],
    })
    first = ConfigStore.load(store_dir).get_guild(GUILD)
    after_first = read_server(store_dir, GUILD)
    second = ConfigStore.load(store_dir).get_guild(GUILD)

    assert first == second
    assert read_server(store_dir, GUILD) == after_first


def test_a_v2_file_is_left_exactly_as_it_is(store_dir):
    """The marker is what stops an already-correct Admin list being demoted."""
    write_server(store_dir, GUILD, {
        "_schema": SERVER_SCHEMA_VERSION,
        ADMIN_ROLES_KEY: ["Boss"],
        VERIFIED_ROLES_KEY: ["Deputy"],
    })
    config = ConfigStore.load(store_dir).get_guild(GUILD)
    assert config[ADMIN_ROLES_KEY] == ["Boss"]
    assert config[VERIFIED_ROLES_KEY] == ["Deputy"]


def test_a_write_by_this_build_survives_a_reload(store_dir):
    """The round trip that would break if set_guild left the marker off.

    A file written without ``_schema`` reads as v1 next start, and the migration
    would move this server's Admins down into Verified — a demotion on a restart
    with nothing to connect it to.
    """
    store = ConfigStore.load(store_dir)
    store.set_guild(GUILD, {ADMIN_ROLES_KEY: ["Boss"]})

    reloaded = ConfigStore.load(store_dir).get_guild(GUILD)
    assert reloaded[ADMIN_ROLES_KEY] == ["Boss"]
    assert reloaded[VERIFIED_ROLES_KEY] == ["mods", "Champion"]


def test_a_server_with_no_stored_file_gets_the_defaults(store_dir):
    config = ConfigStore.load(store_dir).get_guild(GUILD)
    assert config[ADMIN_ROLES_KEY] == ["Commish"]
    assert config[VERIFIED_ROLES_KEY] == ["mods", "Champion"]
    assert config[RESTRICTED_ROLES_KEY] == []


# --- the marker is bookkeeping, not a setting ------------------------------


def test_the_schema_marker_is_not_reported_as_a_setting(store_dir):
    """``/setup show`` and the wizard iterate this dict; the version is not
    something a server configured and must not appear as though it were."""
    write_server(store_dir, GUILD, {"commissioner_roles": ["Commish"]})
    config = ConfigStore.load(store_dir).get_guild(GUILD)
    assert "_schema" not in config
