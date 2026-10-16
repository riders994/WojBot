"""Scheduler cog: the clock behind every scheduled task.

All this owns is a loop. The registry of what to run and the logic for running it
live on the bot as ``bot.tasks`` (:class:`wojbot.core.task_runner.TaskRunner`),
because cogs load in an arbitrary order and the cog that registers a handler
cannot depend on this one having loaded first.

``/scheduler status`` is the owner-only view of whether the clock is running, and
``/scheduler tick`` forces a pass without waiting for the next one -- the two
things worth having when a reminder somebody set did not arrive.

The tick interval is the resolution of the whole system: a task is delivered on
the first tick at or after its due time, so 30 seconds means a reminder is at
most half a minute late. Polling rather than one timer per task is deliberate --
timers do not survive a restart, and there are only ever a handful of tasks due.
"""

from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..core.checks import is_bot_owner
from ..core.schedule import UTC

log = logging.getLogger(__name__)

TICK_SECONDS = 30.0


class Scheduler(commands.Cog):
    """Ticks the task runner."""

    group = app_commands.Group(
        name="scheduler", description="Scheduled task engine (owner only)."
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.last_tick: datetime | None = None
        self.last_count = 0

    async def cog_load(self) -> None:
        self.tick_loop.start()

    async def cog_unload(self) -> None:
        self.tick_loop.cancel()

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick_loop(self) -> None:
        self.last_count = await self.bot.tasks.tick()
        self.last_tick = datetime.now(UTC)

    @tick_loop.before_loop
    async def before_tick(self) -> None:
        # Nothing can be delivered before the gateway is up, and the first tick
        # would otherwise burn a firing on a bot that cannot send anything.
        await self.bot.wait_until_ready()

    @tick_loop.error
    async def on_tick_error(self, exc: Exception) -> None:
        """Restart the loop if a tick ever escapes its own error handling.

        :meth:`TaskRunner.tick` is written not to raise, so reaching here is a
        bug. discord.ext.tasks stops a loop whose body raised, though, and a
        stopped scheduler is silent -- no reminder in any server would ever fire
        again, and nothing would say why. Log it and start again.
        """
        log.exception("Scheduler tick raised; restarting the loop", exc_info=exc)
        self.tick_loop.restart()

    @group.command(name="status", description="Show the scheduler's state.")
    @is_bot_owner()
    async def status(self, interaction: discord.Interaction) -> None:
        runner = self.bot.tasks
        running = self.tick_loop.is_running()
        when = (
            f"<t:{int(self.last_tick.timestamp())}:R>"
            if self.last_tick else "not yet"
        )
        kinds = ", ".join(f"`{kind}`" for kind in runner.kinds) or "none registered"
        await interaction.response.send_message(
            f"**Scheduler**\n"
            f"• Loop: {'running' if running else '**stopped**'} "
            f"(every {TICK_SECONDS:.0f}s)\n"
            f"• Last tick: {when} — {self.last_count} task(s) due\n"
            f"• Handlers: {kinds}",
            ephemeral=True,
        )

    @group.command(name="tick", description="Run a scheduler pass right now.")
    @is_bot_owner()
    async def tick_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        fired = await self.bot.tasks.tick()
        await interaction.followup.send(f"Ran a pass: {fired} task(s) due.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Scheduler(bot))
