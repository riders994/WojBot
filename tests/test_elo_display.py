"""Tests for which members an Elo display shows.

A dynasty frame keeps a row for every manager who has ever played, so a
returning manager picks up where they left off. Displays show only the league
as it is in the season being looked at.
"""

from __future__ import annotations

import pandas as pd

from wojbot.cogs.annuhlitucks import _season_year
from wojbot.core.elo_run import in_league as _in_league


class League:
    def __init__(self, seasons):
        self.seasons = seasons


class System:
    def __init__(self, current):
        self.ratings_config = {"current_sports_year": current}


LEAGUE = League({
    2025: {"league_members": {"stayed": {}, "left": {}}},
    2026: {"league_members": {"stayed": {}, "joined": {}}},
    2027: {},  # configured, never scraped
})
SERIES = pd.Series({"left": 1600.0, "stayed": 1550.0, "joined": 1500.0})


def test_only_that_seasons_members_are_kept_in_order():
    assert list(_in_league(SERIES, LEAGUE, 2026).index) == ["stayed", "joined"]
    assert list(_in_league(SERIES, LEAGUE, 2025).index) == ["left", "stayed"]


def test_a_season_with_no_members_on_file_is_not_emptied():
    assert _in_league(SERIES, LEAGUE, 2027).equals(SERIES)
    assert _in_league(SERIES, LEAGUE, 2099).equals(SERIES)


def test_numeric_ids_match_string_member_keys():
    # Sleeper ids can come back from a CSV as numbers.
    league = League({2026: {"league_members": {"123": {}}}})
    assert list(_in_league(pd.Series({123: 1.0, 456: 2.0}), league, 2026).index) == [123]


def test_no_series_stays_none():
    assert _in_league(None, LEAGUE, 2026) is None


def test_season_year_prefers_the_one_asked_for_then_current_then_latest():
    seasons = {2025: None, 2026: None}
    assert _season_year(System(2026), seasons, 2025) == 2025
    assert _season_year(System(2026), seasons, None) == 2026
    assert _season_year(System(2030), seasons, None) == 2026
    assert _season_year(System(2030), {}, None) is None
