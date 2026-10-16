"""Weekly league processes: the jobs a league runs by itself, once a week.

A **weekly process** is a piece of league upkeep that wants doing every week
without anybody typing a command -- rating the week's Elo, and whatever follows
it. This module is the rules and the storage; :mod:`wojbot.cogs.weekly` is the
Discord surface, and each process's actual work lives with the feature it
belongs to (the Elo one is :func:`wojbot.core.elo_run.weekly_elo`).

**It is the same chrontask machinery reminders use.** One row in
``fantasy_sports.sched_task`` per (server, process), kind ``weekly``, with the
process named in the payload. That is worth spelling out because the alternative
-- a second scheduler with its own table -- would mean two things to keep
running, two places to look when something didn't fire, and a second copy of the
one-firing-at-a-time bookkeeping. Everything here is bookkeeping *around*
:mod:`wojbot.core.tasks`:

* **on/off is the row's status.** ``active`` is on, ``paused`` is off, and a
  removed process is ``cancelled`` -- which keeps the row, so a process that ran
  eleven times before somebody removed it still says so.
* **removing is not deleting**, for the same reason.

**Default off, deliberately.** Adding a process to a server says the league
*has* one; enabling it says the league is ready for it to run unattended. A job
that scrapes a platform and publishes ratings should not start doing that
because somebody was reading the command list.

**Why the day depends on the sport.** A weekly run reads what the platform has
scored, and platforms keep correcting stats after the games finish. Basketball's
corrections have settled by Monday morning; football's have not until Tuesday,
because the late Monday game is still being scored. So a basketball league runs
Monday at 5am and a football league Tuesday at 5am -- local to the league (see
:data:`wojbot.core.tasks.TIMEZONE_KEY`), which is why the schedule is a cron
expression rather than an interval: 5am has to stay 5am across a daylight-saving
change.

The sport comes from the Elo league's ``league_type`` (``nba``, ``nfl``, ...),
so a server that has run ``/commish load`` never has to be told which sport it
plays. One that hasn't gets the basketball default and is told so, and either way
``/weekly add`` takes an explicit day.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time

from . import tasks as task_store
from .elo import ratings_config
from .schedule import UTC, Cron
from .tasks import (
    SCOPE_SERVER,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_PAUSED,
    ScheduledTask,
    TaskError,
)

log = logging.getLogger(__name__)

# The task kind. Must match a row in fantasy_sports.dim_task_kind (seeded by
# leagueSQL's sql/dml/insert_dim_task_kind.sql) -- the task_kind foreign key
# enforces it, and a missing row is a rejected INSERT rather than a bad firing.
KIND_WEEKLY = "weekly"

# Payload key naming which process a row runs. The payload is jsonb, so a
# process that grows options of its own puts them alongside it.
PROCESS_KEY = "process"

# The processes, named here rather than in the code that implements each one, so
# the scheduling surface can talk about them without importing pandas or scipy.
PROCESS_ELO = "elo"
PROCESS_MAXPF = "maxpf"

# 5am local. Late enough that the platform has finished correcting the weekend,
# early enough that the league wakes up to it.
RUN_TIME = time(5, 0)

# How long one process may take before the task runner gives up on it. Far
# longer than the default handler timeout, and it has to be: a weekly process
# scrapes a whole season off a fantasy platform before it computes anything, and
# the run that takes longest is the one nobody is watching. The cost is that a
# tick will not start while one is going, so a reminder due at 5:01 may arrive at
# 5:04 -- which is the right trade for a job that runs once a week.
PROCESS_TIMEOUT = 900.0

# Cron weekdays, 0 = Sunday (see wojbot.core.schedule.Cron).
MONDAY = 1
TUESDAY = 2

# Which day each sport's corrections have settled by.
SPORT_DAYS = {
    "basketball": MONDAY,
    "football": TUESDAY,
}

# What the Elo package's league_type means in terms of a sport. Unknown types
# fall back to basketball -- see DEFAULT_SPORT.
LEAGUE_TYPE_SPORTS = {
    "nba": "basketball",
    "wnba": "basketball",
    "ncaab": "basketball",
    "basketball": "basketball",
    "nfl": "football",
    "ncaaf": "football",
    "football": "football",
}

# The sport a server gets when nothing says otherwise. Basketball because this
# is a fantasy basketball bot first; a football league is told which day it got
# and can say otherwise with `/weekly add day:`.
DEFAULT_SPORT = "basketball"

# Day names as `/weekly add day:` accepts them, in cron's own numbering.
DAY_NAMES = {
    "sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3,
    "thursday": 4, "friday": 5, "saturday": 6,
}
DAY_LABELS = {number: name.capitalize() for name, number in DAY_NAMES.items()}


# --------------------------------------------------------------------------
# The processes themselves
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessOutcome:
    """What one run of a process produced.

    ``announce`` is what stops a weekly job becoming weekly noise: a run that
    found nothing new to do returns False and is recorded without being posted.
    A run that failed doesn't return one of these at all -- it raises, and the
    runner records the reason on the row (see
    :class:`wojbot.core.task_runner.TaskRunner`).
    """

    title: str
    body: str
    announce: bool = True


# A process is handed the bot and its own task row, and works out the rest --
# which league, which channel -- from there.
ProcessRun = Callable[[object, ScheduledTask], Awaitable[ProcessOutcome]]


@dataclass(frozen=True)
class WeeklyProcess:
    """One kind of weekly upkeep: a name, a sentence, and the work.

    ``sports`` limits a process to the sports it makes sense for -- the max-PF
    report is nine-category basketball and means nothing to a football league --
    and empty means any sport. Checked when a server adds one, because the
    alternative is a football league finding out at 5am on a Tuesday.
    """

    name: str
    summary: str
    run: ProcessRun
    sports: tuple[str, ...] = ()

    def suits(self, sport: str) -> bool:
        return not self.sports or sport in self.sports

    @property
    def sports_label(self) -> str:
        """"basketball only" -- for a listing. Empty when it suits any sport."""
        if not self.sports:
            return ""
        return f"{' / '.join(self.sports)} only"


class ProcessRegistry:
    """The processes a server may add, keyed by name.

    Lives on the bot as ``bot.weekly`` and is filled by whichever cog owns each
    process -- the Elo cog registers the Elo one. Same reason the task handler
    registry lives on the bot: cogs load in an arbitrary order, so the cog that
    *offers* a process cannot register it with the cog that schedules it.

    Registering is not the same as running: a process here is one a server
    *could* add, and nothing runs until somebody adds and enables it.
    """

    def __init__(self) -> None:
        self._processes: dict[str, WeeklyProcess] = {}

    def register(self, process: WeeklyProcess) -> None:
        if process.name in self._processes:
            # A cog reloaded at runtime registers again; the new one is wanted.
            log.debug("Replacing the weekly process %r", process.name)
        self._processes[process.name] = process

    def get(self, name: str) -> WeeklyProcess | None:
        return self._processes.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._processes))

    def all(self) -> tuple[WeeklyProcess, ...]:
        return tuple(self._processes[name] for name in self.names)


# --------------------------------------------------------------------------
# Which sport, and therefore which day
# --------------------------------------------------------------------------


def sport_for_league_type(league_type: str | None) -> str:
    """The sport a configured ``league_type`` plays, defaulting to basketball."""
    if not league_type:
        return DEFAULT_SPORT
    return LEAGUE_TYPE_SPORTS.get(str(league_type).strip().lower(), DEFAULT_SPORT)


def league_type_for_guild(bot, guild_id: int | None) -> str | None:
    """This server's Elo ``league_type``, without building an EloSystem.

    Deliberately cheap and side-effect free: this answers "which day?" while
    somebody waits on a slash command, and building an EloSystem scrapes.
    """
    return (ratings_config(bot, guild_id) or {}).get("league_type")


def sport_for_guild(bot, guild_id: int | None) -> str:
    """The sport this server's league plays; basketball when nothing says."""
    return sport_for_league_type(league_type_for_guild(bot, guild_id))


def weekly_cron(weekday: int, timezone: str, at: time = RUN_TIME) -> Cron:
    """"Every <weekday> at <at>, in <timezone>" as a cron schedule.

    A cron rather than a 7-day interval on purpose: an interval anchored at 5am
    drifts an hour off when the clocks change, and a job that exists to run after
    the stat corrections have settled is exactly the job that should not.
    """
    return Cron(
        minute=frozenset({at.minute}),
        hour=frozenset({at.hour}),
        day=None,
        month=None,
        weekday=frozenset({weekday}),
        tz=timezone,
    )


def default_weekday(sport: str) -> int:
    """The day a sport's weekly run happens on."""
    return SPORT_DAYS.get(sport, SPORT_DAYS[DEFAULT_SPORT])


def describe_schedule(task: ScheduledTask) -> str:
    """"Mondays at 05:00 (America/New_York)" -- the schedule as a league reads it.

    :meth:`Cron.describe` renders the raw expression, which is the right answer
    for ``/remind`` (somebody typed a cron and should see the cron back) and the
    wrong one here, where nobody typed anything.
    """
    schedule = task.schedule
    weekday = getattr(schedule, "weekday", None)
    hour = getattr(schedule, "hour", None)
    minute = getattr(schedule, "minute", None)
    if not weekday or not hour or not minute:
        return task.describe_when()
    day = DAY_LABELS.get(min(weekday), "?")
    return f"{day}s at {min(hour):02d}:{min(minute):02d} ({task.timezone})"


# --------------------------------------------------------------------------
# Reading and writing (all of it through wojbot.core.tasks)
# --------------------------------------------------------------------------


def process_name(task: ScheduledTask) -> str | None:
    """Which process a task row runs, or None if the payload doesn't say."""
    name = (task.payload or {}).get(PROCESS_KEY)
    return str(name) if name else None


async def processes_for_guild(bot, guild_id: int) -> dict[str, ScheduledTask]:
    """This server's weekly rows, by process name.

    Live rows only -- ``read_sched_task_guild`` selects active and paused -- so a
    removed process is absent rather than present-and-cancelled, which is what
    lets a league add one again after removing it.
    """
    found: dict[str, ScheduledTask] = {}
    for task in await task_store.tasks_for_guild(bot, guild_id):
        if task.kind != KIND_WEEKLY:
            continue
        name = process_name(task)
        if name is None:
            log.warning("Weekly task %s doesn't name a process", task.task_id)
            continue
        # First wins: rows come back next-firing first, and a duplicate pair
        # (two adds racing) should resolve to the one that fires next.
        found.setdefault(name, task)
    return found


async def process_task(bot, guild_id: int, name: str) -> ScheduledTask | None:
    """One server's row for one process, or None if it hasn't got one."""
    return (await processes_for_guild(bot, guild_id)).get(name)


async def is_enabled(bot, guild_id: int | None, name: str) -> bool:
    """Is this process on for this server?

    Answers False when there is no database to ask. That is the safe direction:
    the callers use this to decide whether a command should hand off to the
    weekly job instead of calculating, and handing off to a job that cannot be
    read is how you get a league whose ratings quietly stop moving.
    """
    if guild_id is None:
        return False
    try:
        task = await process_task(bot, guild_id, name)
    except TaskError as exc:
        log.debug("Couldn't read weekly processes for %s: %s", guild_id, exc)
        return False
    except Exception:  # noqa: BLE001 - a gate must never be the thing that fails
        log.exception("Couldn't read weekly processes for %s", guild_id)
        return False
    return task is not None and task.status == STATUS_ACTIVE


async def add_process(
    bot,
    *,
    name: str,
    guild_id: int,
    channel_id: int,
    owner_id: int,
    weekday: int | None = None,
    at: time = RUN_TIME,
    timezone: str | None = None,
    league_id: int | None = None,
    enabled: bool = False,
) -> ScheduledTask:
    """Add a process to a server's weekly run. Off unless told otherwise.

    ``weekday`` defaults to the sport's day. The row is scoped ``server``
    because that is what it is -- a league-wide post into a channel, set by an
    admin -- even though it tags nobody.
    """
    process = bot.weekly.get(name)
    if process is None:
        known = ", ".join(f"`{known}`" for known in bot.weekly.names) or "none"
        raise TaskError(f"`{name}` isn't a process I know. I have: {known}.")

    sport = sport_for_guild(bot, guild_id)
    if not process.suits(sport):
        raise TaskError(
            f"`{name}` is {process.sports_label}, and this server's league plays "
            f"{sport}. If that's wrong, the league's `league_type` says which "
            "sport it is — `/commish load` re-reads it."
        )
    if await process_task(bot, guild_id, name) is not None:
        raise TaskError(
            f"This server already runs `{name}` weekly. "
            f"`/weekly remove process:{name}` first, or change it with "
            f"`/weekly enable` / `/weekly disable`."
        )

    zone = timezone or task_store.resolve_timezone(bot, guild_id)
    if weekday is None:
        weekday = default_weekday(sport)
    schedule = weekly_cron(weekday, zone, at)

    task = ScheduledTask(
        kind=KIND_WEEKLY,
        scope=SCOPE_SERVER,
        owner_id=owner_id,
        guild_id=guild_id,
        channel_id=channel_id,
        league_id=league_id,
        schedule=schedule,
        payload={PROCESS_KEY: name},
        timezone=zone,
        status=STATUS_ACTIVE if enabled else STATUS_PAUSED,
    )
    # Worked out even for a row that starts paused: a schedule with nothing
    # ahead of it should be refused when it is set, not discovered when somebody
    # turns it on. record_task stores NULL for anything that isn't active.
    task.next_fire_at = schedule.next_fire_after(datetime.now(UTC))
    await task_store.record_task(bot, task)
    return task


async def set_enabled(bot, task: ScheduledTask, enabled: bool) -> ScheduledTask:
    """Turn a process on or off.

    Enabling recomputes the next firing from the schedule rather than restoring
    the one it had when it was turned off, which is what stops a process that
    was off for a month from firing the instant it comes back.
    """
    return await task_store.set_task_status(
        bot, task, STATUS_ACTIVE if enabled else STATUS_PAUSED
    )


async def remove_process(bot, task: ScheduledTask) -> ScheduledTask:
    """Take a process off a server's weekly run, keeping its history."""
    return await task_store.set_task_status(bot, task, STATUS_CANCELLED)


async def run_now(bot, task: ScheduledTask) -> ProcessOutcome:
    """Run one process immediately, outside the schedule.

    The one way a calculation happens on demand once a process is enabled, and
    the reason the interactive commands can hand off rather than keeping a
    second copy of the pipeline. It does **not** touch ``next_fire_at``: forcing
    a run is not the week's run, and should not move the week's run.
    """
    name = process_name(task)
    process = bot.weekly.get(name) if name else None
    if process is None:
        raise TaskError(f"Nothing here knows how to run `{name}`.")
    return await process.run(bot, task)


@dataclass
class ProcessView:
    """A process and this server's row for it, for ``/weekly list``.

    Both halves are optional and each absence means something different: no
    ``task`` is "this server doesn't run it", and no ``process`` is "this row
    names something the bot no longer offers", which is worth showing rather
    than hiding -- it is why a job stopped producing anything.
    """

    name: str
    process: WeeklyProcess | None = None
    task: ScheduledTask | None = None
    summary: str = field(default="")

    @property
    def enabled(self) -> bool:
        return self.task is not None and self.task.status == STATUS_ACTIVE

    @property
    def added(self) -> bool:
        return self.task is not None


async def list_processes(bot, guild_id: int) -> list[ProcessView]:
    """Every process this server could run, with what it has set for each."""
    rows = await processes_for_guild(bot, guild_id)
    views = {
        process.name: ProcessView(
            name=process.name, process=process, summary=process.summary
        )
        for process in bot.weekly.all()
    }
    for name, task in rows.items():
        view = views.get(name)
        if view is None:
            views[name] = ProcessView(
                name=name, task=task, summary="(no longer offered by this bot)"
            )
        else:
            view.task = task
    return [views[name] for name in sorted(views)]
