"""Tests for the weekly max-PF report: which league, which week, and the render.

The metric itself is the ``max-pf`` package's and is not retested here. What is
tested is everything the bot puts around it, which is where the bot can be wrong:

* **which league** -- the Fantrax id is read out of the server's Elo config, and
  every way that can fail has to come back as a sentence an admin can act on.
* **which week** -- the last matchup period that has actually *finished*, worked
  out from the league's schedule. Reporting on a week still being played would
  call a Tuesday-lunchtime scoreline final.
* **the sport gate** -- a nine-category basketball metric must not be addable to
  a football league.

``max_pf.TeamSeason`` is used for the render test rather than a stand-in: the
report leans on its ``potential`` and ``delta`` properties, and a fake would let
a sign error through.
"""

from __future__ import annotations

from datetime import date

import pytest
from max_pf import TeamSeason

from wojbot.core.elo import runtime_key, season_platform
from wojbot.core.maxpf import (
    MAX_NAME,
    REPORT_WINDOW,
    MaxPfError,
    Report,
    latest_finished_period,
    league_id_for_guild,
    maxpf_process,
    query_report,
    render_report,
    weekly_report,
)
from wojbot.core.tasks import TaskError
from wojbot.core.weekly import PROCESS_MAXPF, add_process, sport_for_guild

from .test_tasks import CHANNEL, GUILD, OWNER
from .test_weekly import WeeklyBot

LEAGUE_KEY = "test_league"

NBA_RATINGS = {
    "league_name": "Mao's Macho Mandarins",
    "league_type": "nba",
    "platform": "fantrax",
    "current_sports_year": 2025,
    "seasons": {2024: {"league_id": "old"}, 2025: {"league_id": "wserh14rmbbpqtcg"}},
}
NFL_RATINGS = {
    "league_name": "Die Nasty",
    "league_type": "nfl",
    "platform": "sleeper",
    "current_sports_year": 2026,
    "seasons": {2026: {"league_id": "860329110977785856"}},
}


class LoadedSystem:
    """A loaded EloSystem, as far as the cheap config readers are concerned."""

    def __init__(self, ratings: dict) -> None:
        self.ratings_config = ratings


def bot_with(tmp_path, ratings: dict | None):
    """A bot whose one guild is bound to a league with ``ratings`` loaded."""
    bot = WeeklyBot(tmp_path, guilds={GUILD: {"elo_league": LEAGUE_KEY}})
    if ratings is not None:
        bot.runtime.set(runtime_key(LEAGUE_KEY), LoadedSystem(ratings))
    return bot


# --------------------------------------------------------------------------
# Which league
# --------------------------------------------------------------------------


def test_the_league_id_comes_from_the_current_season(tmp_path):
    bot = bot_with(tmp_path, NBA_RATINGS)
    # The current season's, not the first one on file -- the report follows the
    # league from year to year without anybody re-typing an id.
    assert league_id_for_guild(bot, GUILD) == "wserh14rmbbpqtcg"


def test_a_server_bound_to_no_league_is_told_to_bind_one(tmp_path):
    bot = WeeklyBot(tmp_path)
    with pytest.raises(MaxPfError, match="/commish load"):
        league_id_for_guild(bot, GUILD)


def test_a_league_on_another_platform_is_refused(tmp_path):
    bot = bot_with(tmp_path, NFL_RATINGS)
    with pytest.raises(MaxPfError, match="Fantrax leagues"):
        league_id_for_guild(bot, GUILD)


def test_a_season_pinned_to_another_platform_beats_the_leagues(tmp_path):
    """The same precedence the Elo dims use: a pinned season keeps its platform."""
    moved = {**NBA_RATINGS, "seasons": {2025: {"league_id": "x", "platform": "sleeper"}}}
    assert season_platform(moved, 2025) == "sleeper"
    with pytest.raises(MaxPfError, match="ran on sleeper"):
        league_id_for_guild(bot_with(tmp_path, moved), GUILD)


def test_a_season_with_no_id_on_file_says_which_command_records_one(tmp_path):
    bot = bot_with(tmp_path, {**NBA_RATINGS, "seasons": {2025: {}}})
    with pytest.raises(MaxPfError, match="commish season add"):
        league_id_for_guild(bot, GUILD)


# --------------------------------------------------------------------------
# Which week
# --------------------------------------------------------------------------


class FakePlatform:
    """A league whose matchup periods are three days each, back to back."""

    def __init__(
        self,
        periods: int = 3,
        start: date = date(2026, 1, 5),
        no_results: tuple[int, ...] = (),
    ) -> None:
        self.no_results = set(no_results)
        self._days: dict[int, list[int]] = {}
        self._dates: dict[int, date] = {}
        day = 1
        for period in range(1, periods + 1):
            self._days[period] = []
            for offset in range(3):
                self._dates[day] = date.fromordinal(
                    start.toordinal() + (period - 1) * 3 + offset
                )
                self._days[period].append(day)
                day += 1

    def matchup_periods(self) -> list[int]:
        return sorted(self._days)

    def scoring_dates(self) -> dict[int, date]:
        return dict(self._dates)

    def matchup_period_days(self, period: int) -> list[int]:
        return self._days[period]

    def team_ids(self) -> list[str]:
        return ["t1", "t2"]

    def matchup_opponent(self, team_id: str, period: int) -> str | None:
        """Every period has a matchup bar ``no_results`` -- the playoff case."""
        if period in self.no_results:
            return None
        return "t2" if team_id == "t1" else "t1"


def test_the_week_reported_is_the_last_one_that_finished():
    platform = FakePlatform()  # periods run Jan 5-7, 8-10, 11-13
    # Mid-period-3: period 2 is the last one whose final day is behind us.
    assert latest_finished_period(platform, date(2026, 1, 12)) == 2
    # The morning after period 3's last day, it becomes the one to report.
    assert latest_finished_period(platform, date(2026, 1, 14)) == 3
    # The day period 2 ends is *not* the day it is final.
    assert latest_finished_period(platform, date(2026, 1, 10)) == 1


def test_a_season_that_hasnt_finished_a_week_has_nothing_to_report():
    platform = FakePlatform()
    assert latest_finished_period(platform, date(2026, 1, 6)) is None


def test_a_period_with_no_results_is_skipped_for_the_one_before_it():
    """max-pf reads the regular season, so a playoff period has nothing behind it."""
    platform = FakePlatform(no_results=(3,))
    assert latest_finished_period(platform, date(2026, 1, 14)) == 2


def test_the_weekly_post_goes_quiet_in_the_offseason():
    platform = FakePlatform()
    # The morning after: reported. Three weeks later: nothing, forever -- the
    # window is what stops the process reposting March every Monday.
    assert latest_finished_period(platform, date(2026, 1, 14), REPORT_WINDOW) == 3
    assert latest_finished_period(platform, date(2026, 2, 4), REPORT_WINDOW) is None


def test_a_query_has_no_window_because_july_is_a_fine_time_to_ask():
    platform = FakePlatform()
    assert latest_finished_period(platform, date(2026, 2, 4)) == 3


def test_no_finished_week_means_no_report_and_no_scrape(monkeypatch):
    """The whole run is skipped, rather than max_pf being asked for week None."""
    import max_pf

    monkeypatch.setattr(max_pf, "build_platform", lambda login: FakePlatform())
    monkeypatch.setattr(
        max_pf, "run", lambda *a, **k: pytest.fail("ran the report with no finished week")
    )
    assert weekly_report("league", date(2026, 1, 6), "The league") is None
    assert query_report("league", date(2026, 1, 6), "The league") is None


def test_the_report_asks_for_one_finished_week_of_realized_results(monkeypatch):
    import max_pf

    calls: list = []
    monkeypatch.setattr(max_pf, "build_platform", lambda login: FakePlatform())
    monkeypatch.setattr(
        max_pf, "run",
        lambda login, **kwargs: calls.append((login, kwargs)) or [a_row("A", 5.0, 6.0, 5.0)],
    )

    report = weekly_report("wserh14rmbbpqtcg", date(2026, 1, 14), "Macho Mandarins")

    login, kwargs = calls[0]
    assert login == {"platform": "fantrax", "league_id": "wserh14rmbbpqtcg"}
    assert kwargs["weeks"] == 3          # the finished week, not the season
    assert kwargs["methodology"] == "hindsight"  # the week is over; read what happened
    assert kwargs["nash"] is False       # the season-shaped lens, not the weekly one
    assert report.week == 3
    assert report.title.endswith("max PF, week 3")


def test_teams_that_didnt_play_that_week_are_left_out(monkeypatch):
    """The week the playoffs start, most of the league has no counted matchup."""
    import max_pf

    monkeypatch.setattr(max_pf, "build_platform", lambda login: FakePlatform())
    monkeypatch.setattr(
        max_pf, "run",
        lambda login, **kwargs: [
            a_row("Played", 6.0, 7.0, 5.0),
            a_row("On a bye", 0.0, 0.0, 0.0, periods=0),
        ],
    )
    report = weekly_report("league", date(2026, 1, 14), "The league")
    assert [row.name for row in report.rows] == ["Played"]

    # ...and a week nobody played is no report at all, rather than a table of
    # zeroes with somebody named as having left the most on the table.
    monkeypatch.setattr(
        max_pf, "run", lambda login, **kwargs: [a_row("On a bye", 0.0, 0.0, 0.0, periods=0)]
    )
    assert weekly_report("league", date(2026, 1, 14), "The league") is None


def test_a_season_query_asks_for_every_period_and_says_how_far_it_got(monkeypatch):
    import max_pf

    calls: list = []
    monkeypatch.setattr(max_pf, "build_platform", lambda login: FakePlatform())
    monkeypatch.setattr(
        max_pf, "run",
        lambda login, **kwargs: calls.append(kwargs) or [a_row("A", 12.0, 15.0, 11.0, periods=3)],
    )
    report = query_report("league", date(2026, 1, 14), "The league")

    # weeks=None is how "the season to date" is expressed: max-pf skips a team
    # with no opponent, so the unplayed periods cost nothing.
    assert calls[0]["weeks"] is None
    assert report.season
    assert report.through == 3
    assert report.title == "The league — max PF, season to date (through week 3)"
    # GP earns its column here: the rows are sums over however many weeks.
    assert "GP" in render_report(report)


def test_a_week_query_is_checked_against_the_schedule(monkeypatch):
    import max_pf

    monkeypatch.setattr(max_pf, "build_platform", lambda login: FakePlatform(no_results=(3,)))
    monkeypatch.setattr(max_pf, "run", lambda *a, **k: pytest.fail("shouldn't get that far"))
    today = date(2026, 1, 20)

    with pytest.raises(MaxPfError, match="runs 1–3"):
        query_report("league", today, "The league", week=9)
    with pytest.raises(MaxPfError, match="no regular-season results"):
        query_report("league", today, "The league", week=3)
    # A week still being played is refused rather than half-reported.
    with pytest.raises(MaxPfError, match="isn't finished yet"):
        query_report("league", date(2026, 1, 9), "The league", week=2)


def test_a_week_query_runs_that_week(monkeypatch):
    import max_pf

    calls: list = []
    monkeypatch.setattr(max_pf, "build_platform", lambda login: FakePlatform())
    monkeypatch.setattr(
        max_pf, "run",
        lambda login, **kwargs: calls.append(kwargs) or [a_row("A", 5.0, 6.0, 5.0)],
    )
    report = query_report("league", date(2026, 1, 20), "The league", week=1)

    assert calls[0]["weeks"] == 1
    assert not report.season
    assert report.title == "The league — max PF, week 1"
    # No GP column on a single week: every row is one week by definition.
    assert "GP" not in render_report(report).split("```")[1]


# --------------------------------------------------------------------------
# The render
# --------------------------------------------------------------------------


def a_row(name, actual, m1, m2, periods: int = 1) -> TeamSeason:
    return TeamSeason(
        team_id=name, name=name, periods=periods, actual_pf=actual, m1_pf=m1, m2_pf=m2
    )


def test_the_report_leads_with_who_left_the_most_on_the_table():
    report = Report(
        week=7,
        through=7,
        rows=[
            a_row("Streakbreaker", 6.0, 6.5, 5.0),
            a_row("French Wembassy", 3.0, 8.0, 4.0),  # -5.0: the worst week
            a_row("ZuPacalypse Now", 5.5, 6.0, 1.0),  # delta 5.0: the biggest gift
        ],
        league_name="Macho Mandarins",
    )
    assert report.most_left_on_the_table.name == "French Wembassy"
    assert report.biggest_dividend.name == "ZuPacalypse Now"

    rendered = render_report(report)
    assert "**French Wembassy** left the most on the table this week: 5.0" in rendered
    assert "**ZuPacalypse Now** was handed the most" in rendered
    # Every team is in the table, and the numbers are the package's own.
    for row in report.rows:
        assert row.name[:MAX_NAME] in rendered
    assert "-5.0" in rendered  # potential is signed: it is a shortfall
    assert rendered.count("```") == 2


def test_a_long_team_name_is_cut_rather_than_wrapping():
    long_name = "The Team With A Preposterously Long Name"
    rendered = render_report(
        Report(week=1, through=1, rows=[a_row(long_name, 5.0, 6.0, 5.0)], league_name="L")
    )
    table = rendered.split("```")[1]
    assert long_name not in table
    assert "…" in table
    assert max(len(line) for line in table.splitlines()) <= MAX_NAME + 28


# --------------------------------------------------------------------------
# The sport gate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_football_league_cant_add_the_basketball_report(tmp_path):
    bot = bot_with(tmp_path, NFL_RATINGS)
    bot.weekly.register(maxpf_process())
    assert sport_for_guild(bot, GUILD) == "football"

    with pytest.raises(TaskError, match="basketball only"):
        await add_process(
            bot, name=PROCESS_MAXPF, guild_id=GUILD, channel_id=CHANNEL, owner_id=OWNER
        )
    assert bot.sql.rows == []


@pytest.mark.asyncio
async def test_a_basketball_league_gets_it_on_monday(tmp_path):
    bot = bot_with(tmp_path, NBA_RATINGS)
    bot.weekly.register(maxpf_process())
    assert sport_for_guild(bot, GUILD) == "basketball"

    task = await add_process(
        bot, name=PROCESS_MAXPF, guild_id=GUILD, channel_id=CHANNEL, owner_id=OWNER
    )
    assert task.schedule.weekday == frozenset({1})  # Monday, for the corrections
