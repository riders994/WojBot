"""Tests for weekly league processes: the day, the toggle, and the catch-up run.

No Discord, no Postgres, and no fantasy platform. The storage fakes come from
:mod:`tests.test_tasks` -- a weekly process *is* a chrontask, and testing it
against a second, kinder fake would be testing something the bot doesn't do.

Three things here are worth a test and the rest follows from them:

* **which day.** Basketball Monday, football Tuesday, both at 5am local and both
  still 5am after the clocks change. Get this wrong and the run happens while the
  platform is still correcting the weekend's stats, which is the entire reason
  the feature has a day at all.
* **default off.** A process is added switched off and stored with no next
  firing, so the poller cannot see it until somebody turns it on.
* **catching up.** The unattended run has to work out its own weeks, rate every
  one the platform has scored since the last run, and say "nothing to do" rather
  than failing when there is nothing to do.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from wojbot.core.elo_run import (
    RunResult,
    ScopeReport,
    parse_weeks,
    render_standings,
    run_weeks,
)
from wojbot.core.task_runner import TaskRunner
from wojbot.core.tasks import (
    SCOPE_SERVER,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_PAUSED,
    TaskError,
    task_by_id,
)
from wojbot.core.weekly import (
    KIND_WEEKLY,
    MONDAY,
    TUESDAY,
    PROCESS_ELO,
    ProcessOutcome,
    ProcessRegistry,
    WeeklyProcess,
    add_process,
    default_weekday,
    describe_schedule,
    is_enabled,
    list_processes,
    process_task,
    processes_for_guild,
    remove_process,
    run_now,
    set_enabled,
    sport_for_league_type,
    weekly_cron,
)

from .test_tasks import CHANNEL, GUILD, OWNER, FakeConfigs, FakeSql

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 15, 30, tzinfo=UTC)  # a Friday
TZ = "America/New_York"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeLeagues:
    """The bot's league cache; only ``invalidate`` is reached from here."""

    def __init__(self) -> None:
        self.invalidated: list[int] = []

    def invalidate(self, guild_id: int) -> None:
        self.invalidated.append(guild_id)


class WeeklyBot:
    """A bot with the two registries and the storage the weekly code touches."""

    def __init__(self, tmp_path, guilds: dict[int, dict] | None = None) -> None:
        from wojbot.core.discord_anon import DiscordAnon

        self.sql = FakeSql()
        self.configs = FakeConfigs(guilds or {GUILD: {"timezone": TZ}})
        self.discord_anon = DiscordAnon(tmp_path / "discord_ids.json")
        self.weekly = ProcessRegistry()
        self.leagues = FakeLeagues()
        self.runtime = _Runtime()


class _Runtime:
    def __init__(self) -> None:
        self._values: dict = {}

    def get(self, key):
        return self._values.get(key)

    def set(self, key, value) -> None:
        self._values[key] = value


@pytest.fixture
def bot(tmp_path):
    bot = WeeklyBot(tmp_path)
    bot.weekly.register(a_process())
    return bot


def a_process(name: str = PROCESS_ELO, outcome: ProcessOutcome | None = None):
    """A process that records the tasks it was run for."""
    ran: list = []

    async def run(bot, task):
        ran.append(task)
        return outcome or ProcessOutcome(title="ran", body="it ran")

    process = WeeklyProcess(name=name, summary="a test process", run=run)
    # The dataclass is frozen -- deliberately, a registered process shouldn't be
    # editable in place -- so the record of what it ran is attached around it.
    object.__setattr__(process, "ran", ran)
    return process


async def added(bot, **overrides):
    """One added-and-therefore-switched-off process."""
    defaults = dict(
        name=PROCESS_ELO,
        guild_id=GUILD,
        channel_id=CHANNEL,
        owner_id=OWNER,
    )
    defaults.update(overrides)
    return await add_process(bot, **defaults)


# --------------------------------------------------------------------------
# Which day, and what time
# --------------------------------------------------------------------------


def test_basketball_runs_monday_and_football_tuesday():
    assert default_weekday(sport_for_league_type("nba")) == MONDAY
    assert default_weekday(sport_for_league_type("nfl")) == TUESDAY

    for league_type, weekday in (("nba", "Monday"), ("nfl", "Tuesday")):
        schedule = weekly_cron(default_weekday(sport_for_league_type(league_type)), TZ)
        firing = schedule.next_fire_after(NOW).astimezone(ZoneInfo(TZ))
        assert firing.strftime("%A") == weekday
        assert (firing.hour, firing.minute) == (5, 0)


def test_an_unknown_league_type_gets_the_basketball_day():
    # Including no league at all: a server that has never run /commish load
    # still gets a schedule, and is told which one in the confirmation.
    for league_type in (None, "", "kabaddi"):
        assert default_weekday(sport_for_league_type(league_type)) == MONDAY


def test_five_am_stays_five_am_when_the_clocks_change():
    """The reason the schedule is a cron and not a seven-day interval."""
    schedule = weekly_cron(MONDAY, TZ)
    winter = schedule.next_fire_after(datetime(2027, 1, 8, tzinfo=UTC))
    summer = schedule.next_fire_after(datetime(2027, 7, 8, tzinfo=UTC))
    zone = ZoneInfo(TZ)
    assert winter.astimezone(zone).hour == 5
    assert summer.astimezone(zone).hour == 5
    # ...which means the UTC hour has to move, and does.
    assert winter.hour != summer.hour


@pytest.mark.asyncio
async def test_the_schedule_reads_as_a_day_and_a_time(bot):
    """Nobody typed this schedule, so nobody should be shown a cron expression."""
    assert describe_schedule(await added(bot)) == f"Mondays at 05:00 ({TZ})"

    bot.weekly.register(a_process(name="other"))
    overridden = await added(bot, name="other", weekday=TUESDAY, at=time(19, 30))
    assert describe_schedule(overridden) == f"Tuesdays at 19:30 ({TZ})"


# --------------------------------------------------------------------------
# Adding, removing, and the toggle
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_process_is_added_switched_off(bot):
    task = await added(bot)

    assert task.status == STATUS_PAUSED
    assert task.kind == KIND_WEEKLY
    assert task.scope == SCOPE_SERVER
    assert task.payload == {"process": PROCESS_ELO}

    # Stored with no next firing at all, which is what keeps it out of the
    # poller's partial index until somebody turns it on.
    stored = bot.sql.rows[0]
    assert stored["status"] == STATUS_PAUSED
    assert stored["next_fire_at"] is None
    assert task.next_fire_at is None

    # And stored as surrogates, like every other chrontask.
    assert stored["guild_id"] != GUILD
    assert stored["channel_id"] != CHANNEL
    read_back = await process_task(bot, GUILD, PROCESS_ELO)
    assert (read_back.guild_id, read_back.channel_id) == (GUILD, CHANNEL)


@pytest.mark.asyncio
async def test_a_process_the_bot_doesnt_offer_cant_be_added(bot):
    with pytest.raises(TaskError, match="isn't a process I know"):
        await added(bot, name="nonsense")
    assert bot.sql.rows == []


@pytest.mark.asyncio
async def test_a_server_cant_add_the_same_process_twice(bot):
    await added(bot)
    with pytest.raises(TaskError, match="already runs"):
        await added(bot)
    assert len(bot.sql.rows) == 1


@pytest.mark.asyncio
async def test_turning_it_on_works_out_the_next_run(bot):
    task = await added(bot)
    assert not await is_enabled(bot, GUILD, PROCESS_ELO)

    await set_enabled(bot, task, True)
    assert task.status == STATUS_ACTIVE
    assert task.next_fire_at is not None
    assert task.next_fire_at.astimezone(ZoneInfo(TZ)).strftime("%A") == "Monday"
    assert await is_enabled(bot, GUILD, PROCESS_ELO)


@pytest.mark.asyncio
async def test_turning_it_off_takes_it_out_of_the_poller(bot):
    task = await added(bot)
    await set_enabled(bot, task, True)

    await set_enabled(bot, task, False)
    assert task.status == STATUS_PAUSED
    assert task.next_fire_at is None
    assert not await is_enabled(bot, GUILD, PROCESS_ELO)
    # Still added, though -- off is not gone.
    assert PROCESS_ELO in await processes_for_guild(bot, GUILD)


@pytest.mark.asyncio
async def test_removing_keeps_the_row_and_its_history(bot):
    task = await added(bot)
    await set_enabled(bot, task, True)
    bot.sql.rows[0]["fire_count"] = 11

    await remove_process(bot, task)
    assert task.status == STATUS_CANCELLED
    assert await processes_for_guild(bot, GUILD) == {}

    kept = await task_by_id(bot, task.task_id)
    assert kept.status == STATUS_CANCELLED
    assert kept.fire_count == 11
    # ...and having removed it, a server can add it again.
    assert (await added(bot)).status == STATUS_PAUSED


@pytest.mark.asyncio
async def test_is_enabled_says_no_when_the_store_is_unreachable(bot):
    task = await added(bot)
    await set_enabled(bot, task, True)

    bot.sql = None  # the database went away
    # False rather than an exception: this gates whether a command hands off to
    # the weekly job, and handing off to a job nobody can read is how a league's
    # ratings quietly stop moving.
    assert not await is_enabled(bot, GUILD, PROCESS_ELO)


@pytest.mark.asyncio
async def test_a_row_naming_a_process_the_bot_no_longer_has_is_still_shown(bot):
    bot.weekly.register(a_process(name="retired"))
    await added(bot, name="retired")
    # The cog is gone, the row isn't.
    bot.weekly = ProcessRegistry()

    views = {view.name: view for view in await list_processes(bot, GUILD)}
    assert views["retired"].added
    assert views["retired"].process is None
    assert "no longer offered" in views["retired"].summary


# --------------------------------------------------------------------------
# Running one
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_one_calls_the_registered_process(bot):
    task = await added(bot)
    outcome = await run_now(bot, task)
    assert outcome.title == "ran"
    assert bot.weekly.get(PROCESS_ELO).ran == [task]
    # Forcing a run is not the week's run, so it must not move the week's run.
    assert task.next_fire_at is None
    assert task.status == STATUS_PAUSED


@pytest.mark.asyncio
async def test_running_a_process_with_nothing_to_run_it_says_so(bot):
    task = await added(bot)
    bot.weekly = ProcessRegistry()
    with pytest.raises(TaskError, match="knows how to run"):
        await run_now(bot, task)


@pytest.mark.asyncio
async def test_the_runner_fires_a_weekly_task_and_schedules_the_next_one(bot):
    task = await added(bot)
    await set_enabled(bot, task, True)
    due = task.next_fire_at

    fired: list = []

    async def handler(task):
        fired.append(await run_now(bot, task))

    runner = TaskRunner(bot)
    runner.register(KIND_WEEKLY, handler)
    assert await runner.tick(due) == 1
    assert len(fired) == 1

    after = await task_by_id(bot, task.task_id)
    assert after.status == STATUS_ACTIVE
    assert after.last_error is None
    assert after.fire_count == 1
    # A week later, to the local hour.
    assert (after.next_fire_at - due).days == 7


@pytest.mark.asyncio
async def test_a_kind_can_be_given_its_own_timeout(bot):
    """The weekly kind registers a much longer one; this is that plumbing."""
    import asyncio

    task = await added(bot)
    await set_enabled(bot, task, True)

    async def slow(task):
        await asyncio.sleep(5)

    runner = TaskRunner(bot)
    runner.register(KIND_WEEKLY, slow, timeout=0.01)
    await runner.tick(task.next_fire_at)

    after = await task_by_id(bot, task.task_id)
    assert "timed out" in after.last_error
    # Still active: a run that timed out is not a task that is over.
    assert after.status == STATUS_ACTIVE


# --------------------------------------------------------------------------
# The Elo pipeline the weekly process drives
# --------------------------------------------------------------------------


class FakeFrameManager:
    def __init__(self, frame) -> None:
        self.seasonal_elo = {2026: frame}
        self.dynasty_elo = None
        self.generated: list = []

    def generate(self, year, overwrite=False) -> None:
        self.generated.append((year, overwrite))


class FakeLeague:
    """Just enough EloLeague to rate weeks: a frame and a seed to grow it from.

    ``run`` writes the week's column the way the real engine does -- off the week
    before it -- so a run that rates the wrong weeks, or rates them out of order,
    shows up as the wrong numbers rather than as nothing at all.
    """

    is_dynasty = False
    current_sports_year = 2026

    def __init__(self, rated_through: int = 0, scored: int = 3) -> None:
        members = ["a", "b"]
        frame = pd.DataFrame(index=members)
        for week in range(rated_through + 1):
            frame[f"week_{week}"] = [1500.0 + week, 1500.0 - week]
        self.frame_manager = FakeFrameManager(frame)
        self.seasons = {2026: {"current_season_length": scored}}
        self.loaded = True
        self.scraped: list = []
        self.rated: list = []

    def load(self) -> None:
        self.loaded = True

    def add_league(self, year, overwrite=False) -> None:
        self.scraped.append((year, overwrite))

    def run_prep(self, year) -> None:
        pass

    def run(self, week, year, overwrite) -> None:
        self.rated.append(week)
        frame = self.frame_manager.seasonal_elo[year]
        previous = frame[f"week_{week - 1}"]
        frame[f"week_{week}"] = previous + [10.0, -10.0]


class FakeSystem:
    league = "die_nasty"
    ratings_config = {"league_name": "Die Nasty", "league_type": "nfl"}
    writer_key = "csv"

    def __init__(self, league: FakeLeague) -> None:
        self.elo_league = league
        self.published = 0

    def load_frames(self, _):
        pass

    def publish(self) -> None:
        self.published += 1


def test_a_week_spec_reads_a_week_a_range_or_the_latest():
    assert parse_weeks("5", 10) == [5]
    assert parse_weeks("3:8", 10) == [3, 4, 5, 6, 7, 8]
    assert parse_weeks("3-5", 10) == [3, 4, 5]
    assert parse_weeks(None, 7) == [7]
    with pytest.raises(ValueError, match="hasn't been scored"):
        parse_weeks("11", 10)
    with pytest.raises(ValueError, match="backwards"):
        parse_weeks("8:3", 10)
    with pytest.raises(ValueError, match="No weeks have been scored"):
        parse_weeks(None, 0)


def test_catching_up_rates_every_week_since_the_last_run():
    # Rated through week 1, the platform has scored three: the unattended run
    # has to work out that it owes weeks 2 and 3, in that order.
    system = FakeSystem(FakeLeague(rated_through=1, scored=3))
    result = run_weeks(system, catch_up=True)

    assert system.elo_league.rated == [2, 3]
    assert result.weeks == [2, 3]
    assert result.up_to_date is False
    assert result.span == "weeks 2–3"
    assert system.published == 1
    # Two weeks of +10 on top of week 1's 1501.
    assert result.ratings["a"] == pytest.approx(1521.0)
    # The movement reported is the whole run's, not just the last week's.
    assert result.deltas["a"] == pytest.approx(20.0)
    assert result.deltas["b"] == pytest.approx(-20.0)


def test_catching_up_with_nothing_to_catch_up_on_is_not_a_failure():
    system = FakeSystem(FakeLeague(rated_through=3, scored=3))
    result = run_weeks(system, catch_up=True)

    assert result.up_to_date is True
    assert result.weeks == []
    assert system.elo_league.rated == []
    # Nothing was rated, so nothing is published -- and the standings still say
    # where everyone stands, which is what the report would have shown.
    assert system.published == 0
    assert result.ratings["a"] == pytest.approx(1503.0)


def test_a_named_week_that_would_leave_a_gap_says_which_one_to_run():
    system = FakeSystem(FakeLeague(rated_through=1, scored=5))
    with pytest.raises(ValueError, match="Run `2:4` to catch up"):
        run_weeks(system, spec="4")
    assert system.elo_league.rated == []


def test_a_run_scrapes_before_it_rates():
    """Which week is 'current' is what the platform says now, not what we cached."""
    system = FakeSystem(FakeLeague(rated_through=0, scored=1))
    run_weeks(system, catch_up=True)
    assert system.elo_league.scraped == [(2026, True)]


def test_the_report_shows_the_movement_it_can_and_no_more():
    result = RunResult(
        year=2026,
        weeks=[3],
        reports=[
            ScopeReport("seasonal", pd.Series({"a": 1520.0, "b": 1480.0}), {"a": 20.0})
        ],
        names={"a": "Team A"},
        league_name="Die Nasty",
    )
    rendered = render_standings(result)
    assert "🥇 **Team A** — 1520  (▲ 20)" in rendered
    # No delta and no name for b: rendered by its member id, without a trend.
    assert "**b** — 1480" in rendered
    assert "▲ 20" not in rendered.splitlines()[1]


class FakeDynastyLeague(FakeLeague):
    """A dynasty: one timeline across seasons, and each season's own frame.

    ``run`` behaves as elo_system 2.0.2 does: it rates the dynasty week, then
    the season's own week -- and silently skips the season's when the week
    before it is missing. "gone" played in an earlier season and not this one.
    """

    is_dynasty = True

    def __init__(self, dynasty_through: int, season_through: int, scored: int) -> None:
        super().__init__(rated_through=season_through, scored=scored)
        self.seasons[2026].update(
            dynasty_start_week=50, league_members={"a": {}, "b": {}}
        )
        dynasty = pd.DataFrame(index=["a", "b", "gone"])
        dynasty["week_49"] = [1600.0, 1400.0, 1700.0]
        for week in range(dynasty_through + 1):
            dynasty[f"week_{50 + week}"] = [1550.0 + week, 1450.0 - week, 1650.0]
        self.frame_manager.dynasty_elo = dynasty

    def run(self, week, year, overwrite) -> None:
        self.rated.append(week)
        dynasty = self.frame_manager.dynasty_elo
        if f"week_{50 + week}" not in dynasty or overwrite:
            dynasty[f"week_{50 + week}"] = dynasty[f"week_{49 + week}"] + [5.0, -5.0, 0.0]
        season = self.frame_manager.seasonal_elo[year]
        if f"week_{week - 1}" in season:
            season[f"week_{week}"] = season[f"week_{week - 1}"] + [10.0, -10.0]


def test_a_dynasty_reports_what_the_server_asked_for():
    for setting, scopes in [
        (None, ["seasonal"]),
        ("dynasty", ["dynasty"]),
        ("both", ["seasonal", "dynasty"]),
    ]:
        system = FakeSystem(FakeDynastyLeague(dynasty_through=1, season_through=1, scored=2))
        result = run_weeks(system, spec="2", report=setting)
        assert [r.scope for r in result.reports] == scopes


def test_dynasty_reports_leave_out_managers_no_longer_in_the_league():
    system = FakeSystem(FakeDynastyLeague(dynasty_through=1, season_through=1, scored=2))
    result = run_weeks(system, spec="2", report="dynasty")
    # "gone" has the highest dynasty rating on file, and isn't shown.
    assert list(result.reports[0].ratings.index) == ["a", "b"]
    assert result.reports[0].deltas["a"] == pytest.approx(5.0)


def test_a_named_week_past_a_stale_season_says_which_weeks_to_run():
    # Dynasty rated through week 5, the season's own frame only its seed.
    system = FakeSystem(FakeDynastyLeague(dynasty_through=5, season_through=0, scored=6))
    with pytest.raises(ValueError, match="Run `1:6` to catch up"):
        run_weeks(system, spec="6")
    assert system.elo_league.rated == []


def test_catching_up_repairs_a_stale_season_from_where_it_stopped():
    league = FakeDynastyLeague(dynasty_through=5, season_through=0, scored=6)
    result = run_weeks(FakeSystem(league), catch_up=True, report="both")

    assert league.rated == [1, 2, 3, 4, 5, 6]
    season = league.frame_manager.seasonal_elo[2026]
    assert list(season.columns) == [f"week_{w}" for w in range(7)]
    # Dynasty weeks already on file were left as they were.
    assert league.frame_manager.dynasty_elo["week_55"]["a"] == pytest.approx(1555.0)
    assert [r.scope for r in result.reports] == ["seasonal", "dynasty"]


def test_both_reports_render_under_their_own_headings():
    system = FakeSystem(FakeDynastyLeague(dynasty_through=1, season_through=1, scored=2))
    rendered = render_standings(run_weeks(system, spec="2", report="both"))
    assert rendered.index("**Season**") < rendered.index("**Dynasty**")
