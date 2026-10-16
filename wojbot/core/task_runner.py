"""The thing that actually fires scheduled tasks.

One :class:`TaskRunner` lives on the bot as ``bot.tasks``. Cogs register a
handler for a kind of task at load, the scheduler cog ticks the runner, and the
runner does the same three things every time: read what is due, hand each one to
its handler, and move it on.

**Registration is on the bot, not in the scheduler cog, because cogs load in an
arbitrary order.** ``cogs/reminders.py`` cannot register with ``cogs/scheduler.py``
if the latter has not loaded yet, and alphabetical discovery order is not
something a new cog's author should have to know about. The registry is therefore
built in :class:`~wojbot.bot.WojBotV2` alongside the other storage layers, and
both cogs find it already there.

Firing is at-most-once, and deliberately so. A task is advanced whether its
handler worked or not, and a handler that raises gets its reason recorded on the
row rather than retried. The alternative -- retry until it succeeds -- means a
reminder pointed at a deleted channel posts nothing and retries every tick until
somebody notices, and a reminder that half-succeeded says the same thing twice.
For a nudge in Discord, one attempt and a recorded error is the better failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from . import tasks as task_store
from .schedule import UTC
from .tasks import ScheduledTask

log = logging.getLogger(__name__)

# A firing this far behind is not delivered at all -- the schedule is advanced
# past it instead. The bot having been down for a week is not a reason to
# announce a week-old lineup deadline; the tasks after it are still wanted.
STALE_AFTER = timedelta(hours=24)

# Past this, a delivery says how late it is. Under it the lateness is a tick's
# rounding and nobody needs telling.
LATE_AFTER = timedelta(minutes=5)

# How much of a handler's exception text goes on the row. The column is an
# unbounded varchar; this is about a /remind list that stays readable.
MAX_ERROR_LENGTH = 400

# A handler that hangs -- an HTTP call with no timeout of its own -- would hold
# the tick open and stall every task behind it. This bounds one firing, and is
# sized for the common case: posting a message. A kind whose work is genuinely
# long -- a weekly league process scrapes a platform and rates a season -- says
# so when it registers, rather than every other kind being given room it will
# never need.
HANDLER_TIMEOUT = 30.0

TaskHandler = Callable[[ScheduledTask], Awaitable[None]]


class TaskRunner:
    """Registry of task handlers, and the tick that drives them."""

    def __init__(self, bot) -> None:
        self._bot = bot
        self._handlers: dict[str, TaskHandler] = {}
        self._timeouts: dict[str, float] = {}
        # Guards against a tick starting while the last one is still going: a
        # slow batch would otherwise be read again and delivered twice.
        self._lock = asyncio.Lock()

    def register(
        self, kind: str, handler: TaskHandler, *, timeout: float = HANDLER_TIMEOUT
    ) -> None:
        """Register the handler for a kind of task.

        Replacing an existing handler is allowed and expected -- a cog reloaded
        at runtime registers again, and the new function is the wanted one.

        ``timeout`` bounds one firing of this kind. Raising it is a decision
        about the whole loop, not just this task: a tick will not start while the
        one before it is still going, so a handler given fifteen minutes can hold
        everything else up for fifteen minutes. Worth it for work that genuinely
        takes that long, and worth knowing about.
        """
        if kind in self._handlers:
            log.debug("Replacing the handler for task kind %r", kind)
        self._handlers[kind] = handler
        self._timeouts[kind] = timeout

    @property
    def kinds(self) -> tuple[str, ...]:
        """The task kinds that currently have a handler."""
        return tuple(sorted(self._handlers))

    async def tick(self, now: datetime | None = None) -> int:
        """Fire everything due, and return how many were attempted.

        Never raises. A tick runs on a timer, and an exception escaping it takes
        the loop down with it -- discord.ext.tasks cancels a task whose body
        raises -- which would mean one unreadable row stopping every future
        reminder in every server.
        """
        if self._lock.locked():
            # The previous tick is still working. Skipping is right: whatever it
            # has not got to yet is still due next time.
            log.debug("Skipping a scheduler tick; the previous one is still running")
            return 0

        async with self._lock:
            now = now or datetime.now(UTC)
            try:
                due = await task_store.due_tasks(self._bot, now)
            except task_store.TaskError as exc:
                # No database, or it is down. Expected, and it will be back.
                log.debug("Scheduler tick found no store: %s", exc)
                return 0
            except Exception:
                log.exception("Scheduler tick failed to read due tasks")
                return 0

            for task in due:
                try:
                    await self._fire(task, now)
                except Exception:
                    # _fire handles its own failures; anything reaching here is
                    # a bug in the advance, and the next task still deserves a go.
                    log.exception("Firing task %s failed outright", task.task_id)
            return len(due)

    async def _fire(self, task: ScheduledTask, now: datetime) -> None:
        """Deliver one task and record what happened."""
        error: str | None = None
        due_at = task.next_fire_at
        lateness = (now - due_at) if due_at else timedelta()

        if lateness > STALE_AFTER:
            # Not delivered, but still advanced, so the task carries on from its
            # next real occurrence instead of firing on every tick forever.
            log.info(
                "Task %s was due %s ago; skipping this firing as stale",
                task.task_id, lateness,
            )
            error = f"skipped: was {_describe_lateness(lateness)} late"
        else:
            handler = self._handlers.get(task.kind)
            if handler is None:
                # A kind in the database with nothing to run it: a cog that
                # failed to load, or a row from a newer version of the bot.
                log.warning(
                    "Task %s is of kind %r, which has no handler", task.task_id, task.kind
                )
                error = f"no handler for task kind {task.kind!r}"
            else:
                error = await self._deliver(handler, task, lateness)

        try:
            await task_store.advance_task(self._bot, task, fired_at=now, error=error)
        except Exception:
            # The delivery may well have worked; what failed is recording it.
            # Left active on purpose -- it will be read as due again next tick,
            # which is the one case where a repeat beats losing the task.
            log.exception("Couldn't advance task %s after firing", task.task_id)

    async def _deliver(
        self, handler: TaskHandler, task: ScheduledTask, lateness: timedelta
    ) -> str | None:
        """Run one handler. Returns the error to record, or None if it worked."""
        timeout = self._timeouts.get(task.kind, HANDLER_TIMEOUT)
        try:
            async with asyncio.timeout(timeout):
                await handler(task)
        except asyncio.TimeoutError:
            log.warning("Task %s timed out after %ss", task.task_id, timeout)
            return f"timed out after {timeout:.0f}s"
        except Exception as exc:
            log.exception("Task %s failed", task.task_id)
            return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
        if lateness > LATE_AFTER:
            log.info("Task %s delivered %s late", task.task_id, lateness)
        return None


def _describe_lateness(lateness: timedelta) -> str:
    """"3 days", "5 hours" -- coarse on purpose; this goes in an error message."""
    if lateness >= timedelta(days=1):
        days = lateness.days
        return f"{days} day{'s' if days != 1 else ''}"
    hours = int(lateness.total_seconds() // 3600)
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = int(lateness.total_seconds() // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
