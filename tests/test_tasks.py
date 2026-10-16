"""Tests for scheduled tasks: the rules, the storage round-trip, and the runner.

No Discord and no Postgres. :class:`FakeSql` stands in for
:class:`~wojbot.core.sql.SqlService` with a list of dicts, which is enough to
test the two things that actually go wrong in this layer:

* **the anonymization boundary** -- a task goes in holding real Discord ids and
  has to come back holding the same ones, having been stored as surrogates. A
  mistake here is invisible until a reminder is delivered to the wrong person.
* **the runner's bookkeeping** -- what happens to a task after it fires, fails,
  arrives late, or has no handler at all.

:func:`test_the_read_queries_select_the_columns_the_code_unpacks` is the one test
that reads the real ``.sql`` files. ``_row_to_task`` unpacks rows positionally
against :data:`ROW_COLUMNS`, so a column added to one query and not the others is
a silent mis-mapping rather than an error.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wojbot.core.config import PROJECT_ROOT
from wojbot.core.discord_anon import DiscordAnon
from wojbot.core.schedule import OneShot, parse_schedule
from wojbot.core.task_runner import STALE_AFTER, TaskRunner
from wojbot.core.tasks import (
    DEFAULT_TIMEZONE,
    KIND_REMINDER,
    MAX_MENTIONS,
    MAX_REMINDER_LENGTH,
    ROW_COLUMNS,
    SCOPE_CHANNEL,
    SCOPE_SERVER,
    SCOPE_USER,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    TIMEZONE_KEY,
    ScheduledTask,
    TaskError,
    advance_task,
    due_tasks,
    record_task,
    resolve_timezone,
    set_task_status,
    task_by_id,
    tasks_for_guild,
    tasks_for_owner,
    validate_mentions,
    validate_reminder,
    validate_scope,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 17, 15, 30, tzinfo=UTC)
TZ = "America/New_York"

OWNER = 111111111111111111
GUILD = 222222222222222222
CHANNEL = 333333333333333333
OTHER_USER = 444444444444444444
ROLE = 555555555555555555

QUERY_DIR = PROJECT_ROOT / "sql" / "queries"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeSql:
    """An in-memory stand-in for SqlService, speaking the same query names.

    Stores rows as dicts and returns them as tuples in :data:`ROW_COLUMNS`
    order, which is what the real ``SELECT``s do -- so the positional unpacking
    in ``_row_to_task`` is genuinely under test.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.connection = object()  # anything non-None reads as "connected"
        self.calls: list[str] = []
        self._next_id = 1

    def _tuple(self, row: dict) -> tuple:
        return tuple(row[column] for column in ROW_COLUMNS)

    async def run(self, name: str, params=None, connector=None) -> list:
        self.calls.append(name)
        params = params or {}
        if name == "write_sched_task":
            row = {
                "task_id": self._next_id,
                "last_fired_at": None,
                "fire_count": 0,
                "status": STATUS_ACTIVE,
                "last_error": None,
                "created_at": NOW,
                **params,
            }
            self._next_id += 1
            self.rows.append(row)
            return [(row["task_id"], row["created_at"])]
        if name == "read_sched_task_due":
            due = [
                row for row in self.rows
                if row["status"] == STATUS_ACTIVE
                and row["next_fire_at"] is not None
                and row["next_fire_at"] <= params["now"]
            ]
            due.sort(key=lambda row: row["next_fire_at"])
            return [self._tuple(row) for row in due[: params["limit"]]]
        if name == "read_sched_task_one":
            return [
                self._tuple(row) for row in self.rows
                if row["task_id"] == params["task_id"]
            ]
        if name == "read_sched_task_owned":
            return [
                self._tuple(row) for row in self.rows
                if row["owner_id"] == params["owner_id"]
                and row["status"] in ("active", "paused")
            ]
        if name == "read_sched_task_guild":
            return [
                self._tuple(row) for row in self.rows
                if row["guild_id"] == params["guild_id"]
                and row["status"] in ("active", "paused")
            ]
        if name == "update_sched_task_advance":
            row = self._row(params["task_id"])
            row.update(
                next_fire_at=params["next_fire_at"],
                status=params["status"],
                last_fired_at=params["fired_at"],
                last_error=params["last_error"],
                fire_count=row["fire_count"] + 1,
            )
            return [(row["task_id"], row["status"], row["next_fire_at"])]
        if name == "update_sched_task_status":
            row = self._row(params["task_id"])
            row.update(status=params["status"], next_fire_at=params["next_fire_at"])
            return [(row["task_id"], row["status"], row["next_fire_at"])]
        raise AssertionError(f"unexpected query {name!r}")

    def _row(self, task_id: int) -> dict:
        return next(row for row in self.rows if row["task_id"] == task_id)


class FakeConfigs:
    def __init__(self, guilds: dict[int, dict] | None = None) -> None:
        self._guilds = guilds or {}

    def get_guild(self, guild_id: int) -> dict:
        return {TIMEZONE_KEY: DEFAULT_TIMEZONE, **self._guilds.get(guild_id, {})}


class FakeBot:
    def __init__(self, tmp_path: Path, guilds: dict[int, dict] | None = None) -> None:
        self.sql = FakeSql()
        self.configs = FakeConfigs(guilds)
        self.discord_anon = DiscordAnon(tmp_path / "discord_ids.json")


@pytest.fixture
def bot(tmp_path):
    return FakeBot(tmp_path)


def a_task(**overrides) -> ScheduledTask:
    """A DM reminder, an hour out, with everything overridable."""
    defaults = dict(
        kind=KIND_REMINDER,
        scope=SCOPE_USER,
        owner_id=OWNER,
        schedule=OneShot(NOW + timedelta(hours=1)),
        payload={"text": "set your lineup"},
        timezone=TZ,
        next_fire_at=NOW + timedelta(hours=1),
    )
    defaults.update(overrides)
    return ScheduledTask(**defaults)


def due_at(moment: datetime, **overrides) -> ScheduledTask:
    """A one-shot reminder due at ``moment``, schedule and all.

    Setting ``next_fire_at`` alone would leave the row disagreeing with its own
    schedule, which no real row does -- the due query only returns rows whose
    next_fire_at has passed, and for a one-shot that is the same instant as the
    schedule's. A task built the inconsistent way looks exhausted to the poller
    and unfired to the schedule, and tests written against it prove nothing.
    """
    return a_task(schedule=OneShot(moment), next_fire_at=moment, **overrides)


def recurring(text: str, **overrides) -> ScheduledTask:
    """A recurring reminder, with its first firing already worked out."""
    schedule = parse_schedule(text, now=NOW, tz=TZ)
    return a_task(
        schedule=schedule, next_fire_at=schedule.next_fire_after(NOW), **overrides
    )


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


def test_a_reminder_needs_something_to_say():
    for text in ("", "   ", "\n"):
        with pytest.raises(TaskError, match="something to say"):
            validate_reminder(text)


def test_a_reminder_is_stripped_not_truncated():
    assert validate_reminder("  set your lineup  ") == "set your lineup"
    with pytest.raises(TaskError, match="limit"):
        validate_reminder("x" * (MAX_REMINDER_LENGTH + 1))


def test_a_dm_reminder_has_no_channel():
    validate_scope(SCOPE_USER, guild_id=None, channel_id=None)
    validate_scope(SCOPE_USER, guild_id=GUILD, channel_id=None)  # guild is context
    with pytest.raises(TaskError, match="doesn't post to a channel"):
        validate_scope(SCOPE_USER, guild_id=GUILD, channel_id=CHANNEL)


@pytest.mark.parametrize("scope", [SCOPE_CHANNEL, SCOPE_SERVER])
def test_a_posted_reminder_needs_somewhere_to_post(scope):
    validate_scope(scope, guild_id=GUILD, channel_id=CHANNEL)
    with pytest.raises(TaskError, match="which channel"):
        validate_scope(scope, guild_id=GUILD, channel_id=None)
    with pytest.raises(TaskError, match="which channel"):
        validate_scope(scope, guild_id=None, channel_id=CHANNEL)


def test_only_a_server_reminder_may_tag_other_people():
    """The whole reason /remind server is the privileged one."""
    validate_mentions(SCOPE_SERVER, (OTHER_USER,), (ROLE,))
    validate_mentions(SCOPE_CHANNEL, (), ())
    for scope in (SCOPE_USER, SCOPE_CHANNEL):
        with pytest.raises(TaskError, match="Only a server reminder"):
            validate_mentions(scope, (OTHER_USER,), ())
        with pytest.raises(TaskError, match="Only a server reminder"):
            validate_mentions(scope, (), (ROLE,))


def test_mentions_are_capped():
    users = tuple(range(MAX_MENTIONS + 1))
    with pytest.raises(TaskError, match="limit"):
        validate_mentions(SCOPE_SERVER, users, ())


def test_the_timezone_comes_from_the_server_and_falls_back_to_utc(tmp_path):
    bot = FakeBot(tmp_path, {GUILD: {TIMEZONE_KEY: TZ}})
    assert resolve_timezone(bot, GUILD) == TZ
    # A server that never set one, and a DM with no server at all.
    assert resolve_timezone(bot, 999) == DEFAULT_TIMEZONE
    assert resolve_timezone(bot, None) == DEFAULT_TIMEZONE


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_the_read_queries_select_the_columns_the_code_unpacks():
    """Every read query's SELECT list must match ROW_COLUMNS, in order.

    ``_row_to_task`` zips rows against that tuple positionally. A column added to
    one query and not the rest, or added in a different place, produces tasks
    with plausible values in the wrong fields rather than an error.
    """
    for path in sorted(QUERY_DIR.glob("read_sched_task_*.sql")):
        text = path.read_text()
        select = text[text.index("SELECT") : text.index("FROM")]
        # "SELECT a\n , b\n , c" -> the bare column names.
        columns = tuple(
            re.sub(r"^\s*(SELECT|,)\s*", "", line).strip()
            for line in select.strip().splitlines()
        )
        assert columns == ROW_COLUMNS, f"{path.name} disagrees with ROW_COLUMNS"


def test_the_write_query_binds_every_column_it_names():
    """A named column with no matching placeholder would insert silently wrong.

    rv_pytools registers the file as-is, so this is checkable without a database
    and worth checking: the column list and the VALUES list are written apart
    from each other.
    """
    text = (QUERY_DIR / "write_sched_task.sql").read_text()
    columns = re.findall(r"^\s+[,(]\s*(\w+)$", text, re.MULTILINE)
    placeholders = re.findall(r"%\((\w+)\)s", text)
    assert columns, "no columns found -- has the file's shape changed?"
    assert columns == placeholders


@pytest.mark.asyncio
async def test_a_task_round_trips_through_the_database(bot):
    """Real ids in, surrogates stored, the same real ids back out.

    The one test that matters most in this module: a break here delivers
    somebody's reminder to somebody else.
    """
    task = a_task(
        scope=SCOPE_SERVER,
        guild_id=GUILD,
        channel_id=CHANNEL,
        mention_user_ids=(OTHER_USER,),
        mention_role_ids=(ROLE,),
        schedule=parse_schedule("weekly on monday at 9am", now=NOW, tz=TZ),
        next_fire_at=NOW + timedelta(days=7),
    )
    task_id, _created = await record_task(bot, task)

    stored = bot.sql.rows[0]
    # Nothing real reached the row.
    real_ids = {OWNER, GUILD, CHANNEL, OTHER_USER, ROLE}
    assert stored["owner_id"] not in real_ids
    assert stored["guild_id"] not in real_ids
    assert stored["channel_id"] not in real_ids
    assert not set(stored["mention_ids"]) & real_ids

    back = await task_by_id(bot, task_id)
    assert back is not None
    assert (back.owner_id, back.guild_id, back.channel_id) == (OWNER, GUILD, CHANNEL)
    assert back.mention_user_ids == (OTHER_USER,)
    assert back.mention_role_ids == (ROLE,)
    assert back.text == "set your lineup"
    assert back.timezone == TZ
    # And the schedule itself survived, which is the other half of the row.
    assert back.schedule == task.schedule
    assert back.unknown_mentions == 0


@pytest.mark.asyncio
async def test_a_user_keeps_one_surrogate_across_categories(bot):
    """An owner and a mention of the same person must be the same surrogate.

    Both go through the manager category for exactly this reason -- otherwise one
    person would have two identities in the warehouse.
    """
    first = a_task()
    await record_task(bot, first)
    second = a_task(
        scope=SCOPE_SERVER, guild_id=GUILD, channel_id=CHANNEL,
        mention_user_ids=(OWNER,),
    )
    await record_task(bot, second)
    assert bot.sql.rows[1]["mention_ids"] == [bot.sql.rows[0]["owner_id"]]


@pytest.mark.asyncio
async def test_a_role_and_a_user_with_the_same_id_stay_distinct(bot):
    """Different namespaces, so the same number in each is two surrogates.

    Contrived -- a role id and a user id are never equal in practice -- but it is
    what proves the mention reader is using the namespace rather than guessing.
    """
    shared = 777777777777777777
    task = a_task(
        scope=SCOPE_SERVER, guild_id=GUILD, channel_id=CHANNEL,
        mention_user_ids=(shared,), mention_role_ids=(shared,),
    )
    task_id, _ = await record_task(bot, task)
    assert len(set(bot.sql.rows[0]["mention_ids"])) == 2
    back = await task_by_id(bot, task_id)
    assert back.mention_user_ids == (shared,)
    assert back.mention_role_ids == (shared,)


@pytest.mark.asyncio
async def test_an_unresolvable_mention_is_counted_not_dropped(bot):
    """A surrogate with no map entry still counts, so the total stays honest."""
    task = a_task(scope=SCOPE_SERVER, guild_id=GUILD, channel_id=CHANNEL)
    task_id, _ = await record_task(bot, task)
    bot.sql.rows[0]["mention_ids"] = [123456789]  # never minted here
    back = await task_by_id(bot, task_id)
    assert back.mention_user_ids == () and back.mention_role_ids == ()
    assert back.unknown_mentions == 1
    assert back.mention_count == 1


@pytest.mark.asyncio
async def test_an_unreadable_row_is_skipped_rather_than_raising(bot):
    """One corrupt row must not stop the others from firing."""
    good_id, _ = await record_task(bot, a_task())
    bad_id, _ = await record_task(bot, a_task())
    bot.sql._row(bad_id)["schedule_spec"] = "not a timestamp"
    found = await due_tasks(bot, NOW + timedelta(hours=2))
    assert [task.task_id for task in found] == [good_id]


@pytest.mark.asyncio
async def test_a_task_with_no_next_firing_is_refused(bot):
    with pytest.raises(TaskError, match="no next occurrence"):
        await record_task(bot, a_task(next_fire_at=None))


@pytest.mark.asyncio
async def test_missing_database_says_so(tmp_path):
    bot = FakeBot(tmp_path)
    bot.sql = None
    with pytest.raises(TaskError, match="No database"):
        await tasks_for_owner(bot, OWNER)
    bot.sql = FakeSql()
    bot.sql.connection = None
    with pytest.raises(TaskError, match="database is down"):
        await tasks_for_owner(bot, OWNER)


@pytest.mark.asyncio
async def test_listing_by_owner_and_by_guild(bot):
    mine, _ = await record_task(bot, a_task())
    theirs, _ = await record_task(bot, a_task(owner_id=OTHER_USER))
    here, _ = await record_task(
        bot, a_task(scope=SCOPE_CHANNEL, guild_id=GUILD, channel_id=CHANNEL)
    )
    assert {task.task_id for task in await tasks_for_owner(bot, OWNER)} == {mine, here}
    assert {task.task_id for task in await tasks_for_guild(bot, GUILD)} == {here}
    assert {task.task_id for task in await tasks_for_owner(bot, OTHER_USER)} == {theirs}


@pytest.mark.asyncio
async def test_due_only_returns_what_is_due(bot):
    soon, _ = await record_task(bot, due_at(NOW + timedelta(minutes=1)))
    later, _ = await record_task(bot, due_at(NOW + timedelta(days=1)))
    assert [t.task_id for t in await due_tasks(bot, NOW)] == []
    assert [t.task_id for t in await due_tasks(bot, NOW + timedelta(minutes=2))] == [soon]
    assert len(await due_tasks(bot, NOW + timedelta(days=2))) == 2


@pytest.mark.asyncio
async def test_cancelling_takes_a_task_out_of_the_poller(bot):
    task = a_task()
    await record_task(bot, task)
    await set_task_status(bot, task, STATUS_CANCELLED)
    # next_fire_at has to clear with the status: an active task must know when it
    # fires next, and a cancelled one must be invisible to the due query.
    assert task.next_fire_at is None
    assert bot.sql.rows[0]["next_fire_at"] is None
    assert await due_tasks(bot, NOW + timedelta(days=1)) == []


@pytest.mark.asyncio
async def test_resuming_recomputes_the_next_firing(bot):
    task = recurring("daily at 9am")
    await record_task(bot, task)
    await set_task_status(bot, task, "paused")
    assert task.next_fire_at is None
    await set_task_status(bot, task, STATUS_ACTIVE)
    assert task.next_fire_at is not None and task.next_fire_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_resuming_something_with_nothing_left_says_so(bot):
    """A one-shot paused past its own time can't come back."""
    task = a_task(schedule=OneShot(NOW - timedelta(days=1)))
    task.next_fire_at = NOW + timedelta(hours=1)  # as it was when it was set
    await record_task(bot, task)
    await set_task_status(bot, task, "paused")
    with pytest.raises(TaskError, match="no occurrences left"):
        await set_task_status(bot, task, STATUS_ACTIVE)


# --------------------------------------------------------------------------
# Advancing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_one_shot_is_done_once_it_has_fired(bot):
    task = a_task()
    await record_task(bot, task)
    await advance_task(bot, task, fired_at=task.next_fire_at)
    assert task.status == STATUS_DONE
    assert task.next_fire_at is None
    assert task.fire_count == 1


@pytest.mark.asyncio
async def test_a_recurring_task_stays_active_with_a_new_time(bot):
    task = recurring("daily at 9am")
    await record_task(bot, task)
    first = task.next_fire_at
    await advance_task(bot, task, fired_at=first)
    assert task.status == STATUS_ACTIVE
    assert task.next_fire_at > first


@pytest.mark.asyncio
async def test_a_recurring_task_catches_up_without_repeating(bot):
    """Three days down must produce one firing, not three.

    The next time is computed from *now*, not from the missed slot, so the
    backlog collapses instead of the bot shouting three times on restart.
    """
    task = recurring("daily at 9am")
    await record_task(bot, task)
    # Deliberately not on a 9am boundary: landing exactly on a slot would make
    # the gap to the next one exactly a day and prove nothing about catching up.
    much_later = task.next_fire_at + timedelta(days=3, hours=2)
    await advance_task(bot, task, fired_at=much_later)
    assert task.fire_count == 1
    assert task.next_fire_at > much_later
    assert task.next_fire_at - much_later < timedelta(days=1)


@pytest.mark.asyncio
async def test_a_failed_firing_records_why_and_keeps_going(bot):
    task = recurring("daily at 9am")
    await record_task(bot, task)
    await advance_task(bot, task, fired_at=task.next_fire_at, error="Forbidden")
    assert task.status == STATUS_ACTIVE  # a recurring task gets another go
    assert task.last_error == "Forbidden"
    # And a later success clears it, so it doesn't report an old failure forever.
    await advance_task(bot, task, fired_at=task.next_fire_at)
    assert task.last_error is None


@pytest.mark.asyncio
async def test_a_one_shot_that_failed_is_failed_not_done(bot):
    """It never got delivered, so calling it done would be a lie."""
    task = a_task()
    await record_task(bot, task)
    await advance_task(bot, task, fired_at=task.next_fire_at, error="Forbidden")
    assert task.status == STATUS_FAILED


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_runner_fires_what_is_due_and_advances_it(bot):
    fired: list[ScheduledTask] = []
    runner = TaskRunner(bot)
    runner.register(KIND_REMINDER, lambda task: _record(fired, task))

    await record_task(bot, due_at(NOW - timedelta(minutes=1)))
    await record_task(bot, due_at(NOW + timedelta(days=1)))

    assert await runner.tick(NOW) == 1
    assert [task.text for task in fired] == ["set your lineup"]
    assert bot.sql.rows[0]["status"] == STATUS_DONE
    # The one that wasn't due is untouched.
    assert bot.sql.rows[1]["status"] == STATUS_ACTIVE
    assert bot.sql.rows[1]["fire_count"] == 0


@pytest.mark.asyncio
async def test_a_handler_that_raises_does_not_stop_the_others(bot):
    fired: list[ScheduledTask] = []
    runner = TaskRunner(bot)

    async def handler(task):
        if task.text == "boom":
            raise RuntimeError("channel is gone")
        fired.append(task)

    runner.register(KIND_REMINDER, handler)
    await record_task(
        bot, due_at(NOW - timedelta(minutes=2), payload={"text": "boom"})
    )
    await record_task(bot, due_at(NOW - timedelta(minutes=1)))

    assert await runner.tick(NOW) == 2
    assert [task.text for task in fired] == ["set your lineup"]
    assert bot.sql.rows[0]["status"] == STATUS_FAILED
    assert "channel is gone" in bot.sql.rows[0]["last_error"]
    assert bot.sql.rows[1]["status"] == STATUS_DONE


@pytest.mark.asyncio
async def test_a_kind_with_no_handler_is_recorded_not_retried_forever(bot):
    runner = TaskRunner(bot)  # nothing registered
    await record_task(bot, due_at(NOW - timedelta(minutes=1)))
    assert await runner.tick(NOW) == 1
    assert bot.sql.rows[0]["status"] == STATUS_FAILED
    assert "no handler" in bot.sql.rows[0]["last_error"]


@pytest.mark.asyncio
async def test_a_very_late_firing_is_skipped_but_the_task_carries_on(bot):
    """A week-old lineup deadline is not worth announcing; next week's is."""
    fired: list[ScheduledTask] = []
    runner = TaskRunner(bot)
    runner.register(KIND_REMINDER, lambda task: _record(fired, task))

    task = recurring("daily at 9am")
    await record_task(bot, task)

    much_later = task.next_fire_at + STALE_AFTER + timedelta(hours=1)
    assert await runner.tick(much_later) == 1
    assert fired == []  # not delivered
    assert bot.sql.rows[0]["status"] == STATUS_ACTIVE  # but still scheduled
    assert "skipped" in bot.sql.rows[0]["last_error"]
    assert bot.sql.rows[0]["next_fire_at"] > much_later


@pytest.mark.asyncio
async def test_a_slightly_late_firing_is_still_delivered(bot):
    """The bot having been down for ten minutes must not lose the reminder."""
    fired: list[ScheduledTask] = []
    runner = TaskRunner(bot)
    runner.register(KIND_REMINDER, lambda task: _record(fired, task))
    await record_task(bot, due_at(NOW - timedelta(minutes=10)))
    assert await runner.tick(NOW) == 1
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_a_tick_with_no_database_is_a_no_op(tmp_path):
    """The database being down is expected and temporary; the loop must survive."""
    bot = FakeBot(tmp_path)
    bot.sql = None
    runner = TaskRunner(bot)
    runner.register(KIND_REMINDER, lambda task: None)
    assert await runner.tick(NOW) == 0


@pytest.mark.asyncio
async def test_the_runner_reports_its_registered_kinds(bot):
    runner = TaskRunner(bot)
    assert runner.kinds == ()
    runner.register(KIND_REMINDER, lambda task: None)
    assert runner.kinds == (KIND_REMINDER,)
    # Re-registering replaces rather than duplicating, so a reloaded cog works.
    runner.register(KIND_REMINDER, lambda task: None)
    assert runner.kinds == (KIND_REMINDER,)


async def _record(sink: list, task: ScheduledTask) -> None:
    sink.append(task)
