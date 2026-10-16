"""Scheduled tasks: what one is, who may set it, and reading and writing them.

A **chrontask** is a schedule plus a thing to do when it comes round. The
schedule is :mod:`wojbot.core.schedule`; this module is everything else -- the
row, the three scopes that decide who a firing reaches, and the queries.

Two kinds so far: a **reminder**, which posts a message, and a **weekly** league
process, which does a week's upkeep and reports it (see
:mod:`wojbot.core.weekly`). New kinds are meant to be cheap: a row in
``dim_task_kind``, a handler registered with the bot's :class:`TaskRegistry`, and
nothing else -- no DDL, since the payload is ``jsonb`` and the bot's own database
role cannot run any.

The three scopes are the feature, not an implementation detail:

* :data:`SCOPE_USER` -- goes to the owner's DM. Anyone, from anywhere.
* :data:`SCOPE_CHANNEL` -- posts to a channel and may tag *the owner*. Anyone,
  because tagging yourself somewhere you can already speak is not a privilege.
* :data:`SCOPE_SERVER` -- posts to a channel and may tag *other people*. That is
  the privileged one, gated on the server's admin tier, because it is the only
  scope that can make somebody else's phone buzz.

Split the same way as :mod:`wojbot.core.rumor`: everything above
:func:`resolve_timezone` is pure and directly testable, everything below talks to
the warehouse through :class:`wojbot.core.sql.SqlService`.

**Real ids live in the bot; surrogates live in the database.** A
:class:`ScheduledTask` in memory always holds real Discord ids -- that is what
the cogs need to send anything -- and the conversion happens at the two
boundaries in this module, on the way into :func:`record_task` and on the way out
of :func:`_row_to_task`. Nothing above this line should ever see a surrogate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from .discord_anon import (
    CATEGORY_CHANNEL,
    CATEGORY_MANAGER,
    CATEGORY_ROLE,
    CATEGORY_SERVER,
)
from .schedule import UTC, Schedule, ScheduleError, parse_stored

log = logging.getLogger(__name__)

# Guild setting naming the timezone this server's wall-clock schedules are read
# in. Lives here rather than in the setup cog because the setup cog writes it and
# both reminder commands read it.
TIMEZONE_KEY = "timezone"
DEFAULT_TIMEZONE = "UTC"

# Task kinds. Must match a row in fantasy_sports.dim_task_kind, which the
# task_kind foreign key enforces. The weekly one is named in
# wojbot.core.weekly, next to the processes it runs.
KIND_REMINDER = "reminder"

# Scopes. Must match CK_sched_task_scope.
SCOPE_USER = "user"
SCOPE_CHANNEL = "channel"
SCOPE_SERVER = "server"
SCOPES = (SCOPE_USER, SCOPE_CHANNEL, SCOPE_SERVER)

# Statuses. Must match CK_sched_task_status.
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
# The two a task can be while it still has a future: what /remind list shows and
# what may be cancelled.
LIVE_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED)

# How much text a reminder may carry. Discord's own paragraph TextInput ceiling
# is 4000 and payload is jsonb, so this is the only thing bounding it. Shorter
# than a rumor on purpose: a reminder is a nudge, and the useful ones are short.
MAX_REMINDER_LENGTH = 500

# How many mentions one server task may carry. Past a handful this stops being a
# reminder and becomes a way to make an entire server's phones buzz on a cron.
MAX_MENTIONS = 10

# The column order every read query in sql/queries/read_sched_task_*.sql selects.
# One tuple, because _row_to_task unpacks positionally and four queries have to
# agree with it -- if a column is added there it is added here, and nowhere else.
ROW_COLUMNS = (
    "task_id", "task_kind", "scope", "league_id", "owner_id", "guild_id",
    "channel_id", "mention_ids", "payload", "schedule_kind", "schedule_spec",
    "timezone", "next_fire_at", "last_fired_at", "fire_count", "status",
    "last_error", "created_at",
)

# Ceiling on one tick's worth of due tasks. After an outage the backlog can be
# arbitrarily large, and taking it a slice at a time keeps one bad batch from
# holding the loop open.
DUE_LIMIT = 50


class TaskError(Exception):
    """Something the user needs to fix. The message is meant to be shown to them."""


# --------------------------------------------------------------------------
# The task
# --------------------------------------------------------------------------


@dataclass
class ScheduledTask:
    """One row of ``fantasy_sports.sched_task``, holding **real** Discord ids.

    ``task_id`` is None before the row exists; :func:`record_task` fills it in.
    """

    kind: str
    scope: str
    owner_id: int
    schedule: Schedule
    payload: dict
    timezone: str = DEFAULT_TIMEZONE
    guild_id: int | None = None
    channel_id: int | None = None
    mention_user_ids: tuple[int, ...] = ()
    mention_role_ids: tuple[int, ...] = ()
    league_id: int | None = None
    next_fire_at: datetime | None = None
    last_fired_at: datetime | None = None
    fire_count: int = 0
    status: str = STATUS_ACTIVE
    last_error: str | None = None
    task_id: int | None = None
    created_at: datetime | None = None
    # Surrogates that came back from the database and matched neither the role
    # nor the user namespace -- a task set by a different bot install, or one
    # whose anon map was lost. Kept so the count is still honest when the
    # mentions are rendered; see render_mentions.
    unknown_mentions: int = field(default=0, repr=False)

    @property
    def text(self) -> str:
        """The reminder body. Empty for a kind that doesn't carry one."""
        return self.payload.get("text", "")

    @property
    def recurring(self) -> bool:
        return self.schedule.recurring

    @property
    def mention_count(self) -> int:
        return (
            len(self.mention_user_ids) + len(self.mention_role_ids) + self.unknown_mentions
        )

    def describe_when(self) -> str:
        """The schedule in words, in this task's own timezone."""
        from .schedule import resolve_zone

        try:
            return self.schedule.describe(resolve_zone(self.timezone))
        except ScheduleError:
            # A timezone the host no longer has. Say the schedule anyway --
            # refusing to describe a task is worse than describing it in UTC.
            return self.schedule.describe(resolve_zone(DEFAULT_TIMEZONE))


def validate_reminder(text: str) -> str:
    """The reminder body, stripped, or :class:`TaskError` if it won't do.

    Rejects rather than truncates: half a reminder is worse than being told to
    shorten it, because the half that survives is the half you can already see.
    """
    text = (text or "").strip()
    if not text:
        raise TaskError("A reminder needs something to say.")
    if len(text) > MAX_REMINDER_LENGTH:
        raise TaskError(
            f"That reminder is {len(text)} characters; "
            f"{MAX_REMINDER_LENGTH} is the limit."
        )
    return text


def validate_scope(scope: str, *, guild_id: int | None, channel_id: int | None) -> None:
    """Check a scope against its destination, matching CK_sched_task_destination.

    The same rule the constraint enforces, checked here so the failure is a
    sentence rather than a rejected transaction.
    """
    if scope not in SCOPES:
        raise TaskError(f"`{scope}` isn't a scope I know.")
    if scope == SCOPE_USER:
        if channel_id is not None:
            raise TaskError("A DM reminder doesn't post to a channel.")
        return
    if guild_id is None or channel_id is None:
        raise TaskError("A reminder posted to a server has to say which channel.")


def validate_mentions(scope: str, users: tuple[int, ...], roles: tuple[int, ...]) -> None:
    """Only a server task may tag other people, and only so many of them."""
    total = len(users) + len(roles)
    if total and scope != SCOPE_SERVER:
        raise TaskError(
            "Only a server reminder can tag other people. A channel reminder "
            "tags you, and a DM reminder goes to you."
        )
    if total > MAX_MENTIONS:
        raise TaskError(
            f"That's {total} mentions; {MAX_MENTIONS} is the limit for one reminder."
        )


def resolve_timezone(bot, guild_id: int | None) -> str:
    """The timezone a server's wall-clock schedules are read in.

    UTC when there is no server to ask -- a reminder set in a DM by somebody who
    is in no league server has nothing else to go on, and the confirmation says
    which zone it used so it can be set again with an explicit one.
    """
    if guild_id is None:
        return DEFAULT_TIMEZONE
    return bot.configs.get_guild(guild_id).get(TIMEZONE_KEY) or DEFAULT_TIMEZONE


# --------------------------------------------------------------------------
# Reading and writing (SqlService below this line)
# --------------------------------------------------------------------------


def _require_sql(bot):
    """The SQL service, or a :class:`TaskError` explaining why there isn't one."""
    sql = getattr(bot, "sql", None)
    if sql is None:
        raise TaskError("No database is configured, so reminders can't be stored.")
    if sql.connection is None:
        raise TaskError("The database is down. A bot owner can try `/sql reconnect`.")
    return sql


def _surrogate(bot, category: str, real_id: int | None) -> int | None:
    return None if real_id is None else bot.discord_anon.surrogate(category, real_id)


async def record_task(bot, task: ScheduledTask) -> tuple[int, datetime]:
    """Write a new task and return its id and creation time.

    Sets ``task.task_id`` and ``task.next_fire_at`` on the way through, so the
    caller holds the stored row rather than the draft of it.

    A task may be written already switched off -- a weekly league process is
    added that way -- and then it is stored with no next firing at all, which is
    what keeps it out of the poller's partial index. The schedule is still
    checked for a future first: "this can never fire" is worth hearing when the
    task is set, not when somebody turns it on months later.
    """
    sql = _require_sql(bot)
    if task.next_fire_at is None:
        raise TaskError("That schedule has no next occurrence.")
    next_fire_at = task.next_fire_at if task.status == STATUS_ACTIVE else None

    # The only place real ids turn into surrogates on the way in. Mentions keep
    # their kind by going into different category namespaces, which is how
    # _row_to_task tells a role from a user coming back out.
    mentions = [
        bot.discord_anon.surrogate(CATEGORY_ROLE, role_id)
        for role_id in task.mention_role_ids
    ] + [
        bot.discord_anon.surrogate(CATEGORY_MANAGER, user_id)
        for user_id in task.mention_user_ids
    ]

    rows = await sql.run(
        "write_sched_task",
        {
            "task_kind": task.kind,
            "scope": task.scope,
            "league_id": task.league_id,
            "owner_id": bot.discord_anon.surrogate(CATEGORY_MANAGER, task.owner_id),
            "guild_id": _surrogate(bot, CATEGORY_SERVER, task.guild_id),
            "channel_id": _surrogate(bot, CATEGORY_CHANNEL, task.channel_id),
            "mention_ids": mentions,
            # jsonb takes a JSON string; dumping here keeps psycopg2's adapters
            # out of the domain layer.
            "payload": json.dumps(task.payload),
            "schedule_kind": task.schedule.kind,
            "schedule_spec": task.schedule.spec,
            "timezone": task.timezone,
            "next_fire_at": next_fire_at,
            "status": task.status,
        },
    )
    if not rows:
        raise TaskError("The database accepted that but told me nothing back.")
    task.task_id, task.created_at = int(rows[0][0]), rows[0][1]
    task.next_fire_at = next_fire_at
    return task.task_id, task.created_at


def _row_to_task(bot, row) -> ScheduledTask | None:
    """One database row -> a :class:`ScheduledTask` with real ids.

    None when the row can't be turned into a task -- a corrupted schedule spec,
    or a surrogate this install has no map for. Skipping it is deliberate: the
    scheduler reads a batch at a time and one unreadable row must not stop the
    others from firing. The reason is logged.
    """
    values = dict(zip(ROW_COLUMNS, row))
    anon = bot.discord_anon

    owner_id = anon.real(CATEGORY_MANAGER, values["owner_id"])
    if owner_id is None:
        log.warning(
            "sched_task %s has an owner surrogate this install can't resolve; skipping",
            values["task_id"],
        )
        return None

    try:
        schedule = parse_stored(
            values["schedule_kind"], values["schedule_spec"], values["timezone"]
        )
    except ScheduleError:
        log.exception("sched_task %s has an unreadable schedule", values["task_id"])
        return None

    # A surrogate lives in exactly one category namespace, so which lookup hits
    # is what says whether a mention is a role or a user.
    role_ids: list[int] = []
    user_ids: list[int] = []
    unknown = 0
    for surrogate in values["mention_ids"] or ():
        real = anon.real(CATEGORY_ROLE, surrogate)
        if real is not None:
            role_ids.append(real)
            continue
        real = anon.real(CATEGORY_MANAGER, surrogate)
        if real is not None:
            user_ids.append(real)
        else:
            unknown += 1
    if unknown:
        log.warning(
            "sched_task %s has %d mention(s) this install can't resolve",
            values["task_id"], unknown,
        )

    payload = values["payload"]
    if isinstance(payload, str):  # a driver that hands jsonb back as text
        payload = json.loads(payload)

    return ScheduledTask(
        task_id=int(values["task_id"]),
        kind=values["task_kind"],
        scope=values["scope"],
        league_id=values["league_id"],
        owner_id=owner_id,
        guild_id=anon.real(CATEGORY_SERVER, values["guild_id"]),
        channel_id=anon.real(CATEGORY_CHANNEL, values["channel_id"]),
        mention_user_ids=tuple(user_ids),
        mention_role_ids=tuple(role_ids),
        unknown_mentions=unknown,
        payload=payload or {},
        schedule=schedule,
        timezone=values["timezone"],
        next_fire_at=values["next_fire_at"],
        last_fired_at=values["last_fired_at"],
        fire_count=values["fire_count"],
        status=values["status"],
        last_error=values["last_error"],
        created_at=values["created_at"],
    )


async def _read_tasks(bot, query: str, params: dict) -> list[ScheduledTask]:
    sql = _require_sql(bot)
    rows = await sql.run(query, params)
    tasks = (_row_to_task(bot, row) for row in rows)
    return [task for task in tasks if task is not None]


async def due_tasks(bot, now: datetime, limit: int = DUE_LIMIT) -> list[ScheduledTask]:
    """Every active task due at or before ``now``, soonest first."""
    return await _read_tasks(bot, "read_sched_task_due", {"now": now, "limit": limit})


async def task_by_id(bot, task_id: int) -> ScheduledTask | None:
    """One task, whatever its status, or None if there is no such id."""
    found = await _read_tasks(bot, "read_sched_task_one", {"task_id": task_id})
    return found[0] if found else None


async def tasks_for_owner(bot, user_id: int) -> list[ScheduledTask]:
    """One person's live tasks, wherever they set them."""
    return await _read_tasks(
        bot,
        "read_sched_task_owned",
        {"owner_id": bot.discord_anon.surrogate(CATEGORY_MANAGER, user_id)},
    )


async def tasks_for_guild(bot, guild_id: int) -> list[ScheduledTask]:
    """Every live task pointed at one server."""
    return await _read_tasks(
        bot,
        "read_sched_task_guild",
        {"guild_id": bot.discord_anon.surrogate(CATEGORY_SERVER, guild_id)},
    )


async def advance_task(
    bot,
    task: ScheduledTask,
    *,
    fired_at: datetime,
    error: str | None = None,
) -> ScheduledTask:
    """Record that a task fired and work out when it fires next.

    Called once per firing, whether it worked or not. The next firing is
    computed from the schedule rather than added to the last one, which is what
    keeps a cron task on its wall-clock time across a daylight-saving change and
    an interval task from drifting when a delivery ran late.

    A schedule with nothing left ahead of it becomes ``done`` -- or ``failed``,
    if the firing that exhausted it was also the one that broke, since a one-shot
    that never got delivered did not finish.
    """
    sql = _require_sql(bot)
    upcoming = task.schedule.next_fire_after(fired_at)
    if upcoming is not None:
        status = STATUS_ACTIVE
    else:
        status = STATUS_FAILED if error else STATUS_DONE

    await sql.run(
        "update_sched_task_advance",
        {
            "task_id": task.task_id,
            "next_fire_at": upcoming,
            "status": status,
            "fired_at": fired_at,
            # Passed every time, NULL included, so a task that recovers stops
            # reporting the failure before it.
            "last_error": error,
        },
    )
    task.next_fire_at = upcoming
    task.status = status
    task.last_fired_at = fired_at
    task.fire_count += 1
    task.last_error = error
    return task


async def set_task_status(bot, task: ScheduledTask, status: str) -> ScheduledTask:
    """Cancel, pause, or resume a task.

    ``next_fire_at`` moves with the status because the two are tied together by
    ``CK_sched_task_next_fire``: an active task must know when it fires next, and
    anything else must be out of the poller's index. Resuming therefore
    recomputes the next firing from the schedule -- a task paused over its only
    occurrence comes back with nothing ahead of it, and says so.
    """
    sql = _require_sql(bot)
    if status == STATUS_ACTIVE:
        upcoming = task.schedule.next_fire_after(datetime.now(UTC))
        if upcoming is None:
            raise TaskError(
                f"Reminder `#{task.task_id}` has no occurrences left, so there's "
                "nothing to resume. Set a new one."
            )
    else:
        upcoming = None

    await sql.run(
        "update_sched_task_status",
        {"task_id": task.task_id, "status": status, "next_fire_at": upcoming},
    )
    task.status = status
    task.next_fire_at = upcoming
    return task
