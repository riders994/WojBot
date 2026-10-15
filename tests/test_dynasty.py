"""Tests for :mod:`wojbot.core.dynasty`.

The conversion itself is elo_system's, so it isn't re-tested here. What is: the
bot's checks before handing over, and the one thing it changes underneath the
package -- holding the current season's length at what has been rated, so a
schedule that runs ahead of the games doesn't get replayed as results. The
league and system are fakes shaped like the parts of elo_system these functions
touch; a real conversion scrapes a platform.
"""

from __future__ import annotations

import pandas as pd
import pytest

from wojbot.core import dynasty


def frame(last_week: int) -> pd.DataFrame:
    return pd.DataFrame(
        {f"week_{w}": [1500.0, 1500.0] for w in range(last_week + 1)}, index=["a", "b"]
    )


class FakeFrameManager:
    def __init__(self, seasonal: dict, dynasty_elo=None):
        self.seasonal_elo = seasonal
        self.dynasty_elo = dynasty_elo
        self.is_dynasty = False
        self.generated: list = []

    def set_is_dynasty(self, to):
        self.is_dynasty = to

    def has_data(self):
        return any(f is not None for f in self.seasonal_elo.values())

    def generate(self, year, overwrite=False):
        self.generated.append(year)
        self.seasonal_elo.setdefault(year, frame(0))

    def validate_season(self, year):
        return year in self.seasonal_elo


class FakeLeague:
    def __init__(self, seasons: dict, fm: FakeFrameManager, current: int, is_dynasty=False):
        self.seasons = seasons
        self.frame_manager = fm
        self.current_sports_year = current
        self.is_dynasty = is_dynasty
        self.loaded = True
        self.scraped: list = []
        self.ran: list = []
        self.lengths_seen: dict = {}
        self.dumped = 0

    def add_league(self, year):
        self.scraped.append(year)

    def run_season(self, year):
        self.ran.append(year)

    def change_dynasty(self, to):
        # What the replay would rate: 0..current_season_length of every season.
        self.lengths_seen = {y: s.get("current_season_length") for y, s in self.seasons.items()}
        self.is_dynasty = to
        if to:
            start = 0
            for year in sorted(self.seasons):
                self.seasons[year]["dynasty_start_week"] = start
                start += self.seasons[year]["current_season_length"] + 1
            self.frame_manager.dynasty_elo = frame(start - 1)
        return self.is_dynasty

    def dump(self):
        self.dumped += 1


class FakeSystem:
    def __init__(self, league):
        self.elo_league = league
        self.published = 0
        self.written: list = []

    def load_frames(self, which):
        raise AssertionError("frames are already loaded in these tests")

    def publish(self):
        self.published += 1

    def write_configs(self, which):
        self.written.append(which)


def league_2024_to_2026(*, current_rated=0, current_length=25, **kwargs):
    seasons = {
        2024: {"current_season_length": 24},
        2025: {"current_season_length": 24},
        2026: {"current_season_length": current_length},
    }
    fm = FakeFrameManager({2024: frame(24), 2025: frame(24), 2026: frame(current_rated)})
    return FakeLeague(seasons, fm, 2026, **kwargs)


def test_replay_stops_at_the_last_rated_week_of_the_current_season():
    league = league_2024_to_2026(current_rated=3)
    system = FakeSystem(league)

    result = dynasty.enable(system)

    # The platform says 25 weeks; only 3 have been rated, so only 3 are replayed.
    assert league.lengths_seen == {2024: 24, 2025: 24, 2026: 3}
    # ...and the platform's figure is back once the conversion is done.
    assert league.seasons[2026]["current_season_length"] == 25
    assert result.enabled and result.start_weeks == {2024: 0, 2025: 25, 2026: 50}
    assert result.last_week == 53
    assert system.published == 1 and system.written == ["ratings"]
    assert league.dumped == 1


def test_length_is_restored_even_when_the_conversion_fails():
    league = league_2024_to_2026(current_rated=3)

    def boom(to):
        raise RuntimeError("scrape failed")

    league.change_dynasty = boom
    with pytest.raises(RuntimeError):
        dynasty.enable(FakeSystem(league))
    assert league.seasons[2026]["current_season_length"] == 25


def test_every_season_is_scraped_and_given_a_frame_first():
    league = league_2024_to_2026()
    del league.frame_manager.seasonal_elo[2025]  # a past season never rated
    del league.frame_manager.seasonal_elo[2026]  # the current one never seeded

    dynasty.enable(FakeSystem(league))

    assert league.scraped == [2024, 2025, 2026]
    assert league.ran == [2025]
    assert league.frame_manager.generated == [2026]


def test_a_stale_frame_manager_flag_is_brought_into_line():
    # The package won't convert a league whose frame manager already thinks it
    # is a dynasty; the league's own flag is the one on file.
    league = league_2024_to_2026()
    league.frame_manager.is_dynasty = True
    seen = {}
    original = league.change_dynasty

    def spy(to):
        seen["fm"] = league.frame_manager.is_dynasty
        return original(to)

    league.change_dynasty = spy
    dynasty.enable(FakeSystem(league))
    assert seen["fm"] is False


def test_a_league_with_no_ratings_just_gets_the_flag():
    league = league_2024_to_2026()
    league.frame_manager.seasonal_elo.clear()
    system = FakeSystem(league)

    result = dynasty.enable(system)

    assert result.enabled and result.flag_only
    assert league.frame_manager.is_dynasty is True
    assert league.scraped == [] and system.published == 0
    assert system.written == ["ratings"]


def test_already_a_dynasty_is_refused():
    with pytest.raises(ValueError, match="already a dynasty"):
        dynasty.enable(FakeSystem(league_2024_to_2026(is_dynasty=True)))


def test_a_gap_between_seasons_is_named():
    league = league_2024_to_2026()
    del league.seasons[2025]
    with pytest.raises(ValueError, match="2025"):
        dynasty.enable(FakeSystem(league))


def test_disable_keeps_the_dynasty_frame_and_clears_both_flags():
    league = league_2024_to_2026(is_dynasty=True)
    league.frame_manager.is_dynasty = True
    kept = league.frame_manager.dynasty_elo = frame(50)
    system = FakeSystem(league)

    result = dynasty.disable(system)

    assert not result.enabled
    assert league.frame_manager.is_dynasty is False
    assert league.frame_manager.dynasty_elo is kept
    assert system.written == ["ratings"]


def test_disable_refuses_a_league_that_is_not_one():
    with pytest.raises(ValueError, match="isn't a dynasty"):
        dynasty.disable(FakeSystem(league_2024_to_2026()))


@pytest.mark.parametrize(
    "f, expected", [(None, 0), (frame(0), 0), (frame(7), 7)]
)
def test_last_seasonal_week(f, expected):
    assert dynasty.last_seasonal_week(f) == expected


def test_last_seasonal_week_stops_at_a_gap():
    f = frame(5).drop(columns=["week_3"])
    assert dynasty.last_seasonal_week(f) == 2


# --- rating the season alongside the dynasty --------------------------------


class SeasonLeague:
    """Just what rate_season_alongside touches, recording each week rated."""

    league_type, k, scoring = "nfl", 60, "median"

    def __init__(self, rated_through: int):
        self.frame_manager = FakeFrameManager({2026: frame(rated_through)})
        self.rated: list = []
        self.current_league = self

    def get_week(self, week):
        return {"week": week}

    def formatter(self, league_type, scoreboard):
        return dict(scoreboard)

    def _rename(self, scores, year):
        return "renamed"

    def calculator(self, elo_frame, scores, week, overwrite, k, scoring):
        assert elo_frame is self.frame_manager.seasonal_elo[2026]
        assert scores == {"week": week, "opponent": "renamed"}
        self.rated.append((week, overwrite))
        elo_frame[f"week_{week}"] = 1500.0


def test_the_season_catches_up_from_where_it_stopped():
    league = SeasonLeague(rated_through=0)
    dynasty.rate_season_alongside(league, 2026, [4, 5])
    assert league.rated == [(w, False) for w in range(1, 6)]


def test_overwrite_rerates_only_the_weeks_asked_for():
    league = SeasonLeague(rated_through=5)
    dynasty.rate_season_alongside(league, 2026, [4, 5], overwrite=True)
    assert league.rated == [(4, True), (5, True)]


def test_week_zero_rates_nothing():
    league = SeasonLeague(rated_through=0)
    dynasty.rate_season_alongside(league, 2026, [0])
    assert league.rated == []
