"""Tests for how ``/commish load`` picks a league (:mod:`wojbot.core.elo`).

A server is tied to its league twice: ``dim_league.discord_server_id`` (which
rumors read) and the per-guild ``elo_league`` key (which Elo reads). They are
set by different commands, so they can disagree -- and did, leaving a server's
rumors on one league and its ratings on another. These pin the rule that the
database link wins, and how a database league is matched back to a configured
Elo league.
"""

from __future__ import annotations

import asyncio

import pytest

from wojbot.core import elo
from wojbot.core.elo import DatabaseLink, matching_league_keys, plan_load, season_ids

DIE_NASTY = {
    "platform": "sleeper",
    "seasons": {
        2025: {"league_id": "1196653288641003520"},
        2026: {"league_id": "1327122339342606336"},
    },
}
MACHO = {
    "platform": "fantrax",
    "seasons": {
        2025: {"league_id": "wserh14rmbbpqtcg", "platform": "fantrax"},
        2026: {"league_id": "4bstyhwtmqwot8j5"},
    },
}
CONFIGS = {"die_nasty": DIE_NASTY, "macho_mandarins": MACHO}

DIE_NASTY_LINK = DatabaseLink(0, "League Free, Die Nasty", "die_nasty")


# --- matching a database league to a configured one -------------------------


def test_a_season_falls_back_to_the_league_platform():
    assert season_ids(DIE_NASTY) == {
        ("sleeper", "1196653288641003520"),
        ("sleeper", "1327122339342606336"),
    }


def test_league_ids_are_compared_as_text():
    # YAML may read a numeric id as an int; the database column is varchar.
    config = {"platform": "sleeper", "seasons": {2022: {"league_id": 860329110977785856}}}
    assert matching_league_keys([("sleeper", "860329110977785856")], {"x": config}) == ["x"]


def test_any_one_season_is_enough_to_match():
    assert matching_league_keys([("fantrax", "4bstyhwtmqwot8j5")], CONFIGS) == [
        "macho_mandarins"
    ]


def test_the_platform_has_to_match_too():
    # A league id is only unique within its platform.
    assert matching_league_keys([("sleeper", "4bstyhwtmqwot8j5")], CONFIGS) == []


# --- deciding what /commish load loads --------------------------------------


def test_unnamed_load_follows_the_database_link_and_says_it_rebound():
    # The Die Nasty server: linked to Die Nasty, Elo bound to Macho Mandarins.
    plan = plan_load(None, "macho_mandarins", DIE_NASTY_LINK)
    assert plan.refusal is None
    assert plan.key == "die_nasty"
    assert "macho_mandarins" in plan.notes[0]


def test_unnamed_load_on_a_consistent_server_has_nothing_to_say():
    plan = plan_load(None, "die_nasty", DIE_NASTY_LINK)
    assert plan.key == "die_nasty" and plan.notes == [] and not plan.uses_default


def test_naming_the_linked_league_is_fine():
    assert plan_load("die_nasty", None, DIE_NASTY_LINK).key == "die_nasty"


def test_naming_a_different_league_than_the_link_is_refused():
    plan = plan_load("macho_mandarins", "die_nasty", DIE_NASTY_LINK)
    assert plan.key is None
    assert "/commish db migrate league: macho_mandarins" in plan.refusal


def test_an_unlinked_server_keeps_the_old_fallbacks():
    assert plan_load(None, "die_nasty", None).key == "die_nasty"
    assert plan_load("macho_mandarins", "die_nasty", None).key == "macho_mandarins"
    default = plan_load(None, None, None)
    assert default.key is None and default.uses_default


def test_a_link_to_an_unconfigured_league_never_falls_to_the_default():
    link = DatabaseLink(7, "Somebody Else's League", None)
    plan = plan_load(None, None, link)
    assert plan.refusal is not None and not plan.uses_default
    named = plan_load("die_nasty", None, link)
    assert named.key == "die_nasty" and "Somebody Else's League" in named.notes[0]


# --- reading the link -------------------------------------------------------


class FakeAnon:
    def surrogate(self, category, real_id):
        return real_id + 1000

    def real(self, category, surrogate):
        return surrogate - 1000


class FakeSql:
    def __init__(self, leagues: dict, platform_ids: dict):
        self.leagues = leagues  # server surrogate -> (league_id, name)
        self.platform_ids = platform_ids  # league_id -> [(platform, id)]

    async def run(self, name, params):
        if name == "read_league_by_server":
            found = self.leagues.get(params["server_surrogate"])
            return [(*found, params["server_surrogate"])] if found else []
        if name == "read_league_platform_ids":
            return self.platform_ids.get(params["league_id"], [])
        raise AssertionError(name)


class FakeBot:
    def __init__(self, sql):
        self.sql = sql
        self.discord_anon = FakeAnon()


@pytest.fixture
def configs(monkeypatch):
    monkeypatch.setattr(elo, "read_ratings_configs", lambda: CONFIGS)


def run(coro):
    return asyncio.run(coro)


def test_database_league_resolves_the_linked_config(configs):
    sql = FakeSql(
        {1990: (0, "League Free, Die Nasty")},
        {0: [("sleeper", "1327122339342606336")]},
    )
    link = run(elo.database_league(FakeBot(sql), 990))
    assert link == DatabaseLink(0, "League Free, Die Nasty", "die_nasty")


def test_an_unlinked_server_has_no_link(configs):
    assert run(elo.database_league(FakeBot(FakeSql({}, {})), 990)) is None


def test_no_database_means_no_link(configs):
    assert run(elo.database_league(FakeBot(None), 990)) is None


def test_two_configs_claiming_one_league_match_neither(monkeypatch):
    monkeypatch.setattr(
        elo, "read_ratings_configs", lambda: {"a": DIE_NASTY, "b": DIE_NASTY}
    )
    sql = FakeSql({1990: (0, "Die Nasty")}, {0: [("sleeper", "1327122339342606336")]})
    assert run(elo.database_league(FakeBot(sql), 990)).key is None
