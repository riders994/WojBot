"""Weekly cog: the league's standing jobs — what runs, when, and whether it's on.

``/weekly`` is the admin surface over :mod:`wojbot.core.weekly`: add a process to
this server's weekly run, take one off, turn one on or off, and force one now.
The processes themselves are registered by the cogs that own them (the Elo one by
``cogs/annuhlitucks.py``), and the firing is the ordinary scheduler — a weekly
process is a chrontask like a reminder, and ``/scheduler status`` is still where
you look when nothing has happened.

**Adding and enabling are two separate acts, and the second is the deliberate
one.** ``/weekly add`` says the league has this process; it starts switched off.
``/weekly enable`` says it may run unattended, from now on, without anybody
watching it — which for a job that scrapes a platform and publishes ratings is a
decision worth making on purpose rather than by adding it.

The day is worked out from the sport (basketball Monday, football Tuesday, both
at 5am local, so the platform's stat corrections have settled first), and
``day:``/``time:`` override it for a league that knows better.

**A firing posts into a channel.** That is what makes it a report rather than a
cron job nobody can see: if the weekly Elo run has been failing for three weeks,
the league finds out by the report not appearing, and an admin finds out why in
``/weekly list``, where the last error is kept.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..core import weekly as weekly_store
from ..core.checks import is_admin, is_verified
from ..core.rumor import league_for_guild
from ..core.schedule import ScheduleError, parse_time_of_day
from ..core.tasks import STATUS_ACTIVE, ScheduledTask, TaskError
from ..core.weekly import (
    DAY_LABELS,
    KIND_WEEKLY,
    PROCESS_TIMEOUT,
    RUN_TIME,
    ProcessView,
    describe_schedule,
)

log = logging.getLogger(__name__)

# One colour for the reports so a league can tell the weekly post from anything
# else the bot says in that channel.
REPORT_COLOUR = discord.Colour.dark_teal()

DAY_CHOICES = [
    app_commands.Choice(name=label, value=number)
    for number, label in sorted(DAY_LABELS.items())
]


class Weekly(commands.Cog):
    """The ``/weekly`` commands, and the handler that fires the processes."""

    group = app_commands.Group(
        name="weekly",
        description="The league's weekly processes.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Registered against bot.tasks rather than the scheduler cog, which may
        # not have loaded yet -- see wojbot.core.task_runner. The long timeout is
        # the point of it being weekly: a run scrapes a season.
        bot.tasks.register(KIND_WEEKLY, self.fire, timeout=PROCESS_TIMEOUT)

    # --- firing ------------------------------------------------------------

    async def fire(self, task: ScheduledTask) -> None:
        """Run one weekly process and post what it produced.

        Raises on failure, which the runner records on the row -- so a process
        that has been failing says why in ``/weekly list`` instead of just
        having gone quiet.
        """
        outcome = await weekly_store.run_now(self.bot, task)
        if not outcome.announce:
            log.info(
                "Weekly %s for guild %s had nothing to announce: %s",
                weekly_store.process_name(task), task.guild_id, outcome.title,
            )
            return
        await self._post(task, outcome)

    async def _post(self, task: ScheduledTask, outcome) -> None:
        channel = self.bot.get_channel(task.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(task.channel_id)
        embed = discord.Embed(
            title=outcome.title, description=outcome.body, colour=REPORT_COLOUR
        )
        embed.set_footer(
            text=f"weekly {weekly_store.process_name(task)} · #{task.task_id}"
        )
        # Nothing here tags anybody: a weekly report is something a league reads
        # when it wakes up, not something that should buzz twelve phones at 5am.
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # --- reading -----------------------------------------------------------

    @group.command(name="list", description="What this server runs weekly, and when.")
    @is_verified()
    async def list_processes(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            views = await weekly_store.list_processes(self.bot, interaction.guild_id)
        except TaskError as exc:
            await interaction.followup.send(str(exc))
            return
        if not views:
            await interaction.followup.send(
                "This bot offers no weekly processes, which shouldn't happen — "
                "a cog probably failed to load. `/scheduler status` says which."
            )
            return

        sport = weekly_store.sport_for_guild(self.bot, interaction.guild_id)
        embed = discord.Embed(
            title="Weekly processes",
            description=(
                f"This server plays **{sport}**, so a new process runs "
                f"**{DAY_LABELS[weekly_store.default_weekday(sport)]}s** at "
                f"{RUN_TIME.strftime('%H:%M')} by default — after the platform's "
                "stat corrections have settled."
            ),
            colour=REPORT_COLOUR,
        )
        for view in views:
            embed.add_field(
                name=_view_title(view, sport), value=_view_body(view), inline=False
            )
        await interaction.followup.send(embed=embed)

    # --- adding and removing ------------------------------------------------

    @group.command(name="add", description="Add a process to this server's weekly run.")
    @app_commands.describe(
        process="Which process to add",
        channel="Where its report gets posted",
        day="Which day it runs (defaults to the sport's day)",
        time="What time it runs, in this server's timezone (default 05:00)",
    )
    @app_commands.choices(day=DAY_CHOICES)
    @is_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        process: str,
        channel: discord.TextChannel,
        day: app_commands.Choice[int] | None = None,
        time: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            at = parse_time_of_day(time) if time else RUN_TIME
        except ScheduleError as exc:
            await interaction.followup.send(str(exc))
            return

        # Checked now rather than discovered at 5am on Monday: a process whose
        # report cannot be posted is a process that looks like it never ran.
        me = interaction.guild.me if interaction.guild is not None else None
        if me is not None and not channel.permissions_for(me).send_messages:
            await interaction.followup.send(
                f"I can't post in {channel.mention}, so a report there would "
                "never arrive. Give me **Send Messages** there, or pick "
                "another channel."
            )
            return

        # Best effort: the warehouse league this server plays, so the stored row
        # says which league the process belongs to. Nothing here needs it, and a
        # server that has not run `/commish db migrate` yet still gets a process.
        league_id = None
        try:
            league = await league_for_guild(self.bot, interaction.guild_id)
            league_id = league.id if league is not None else None
        except Exception:  # noqa: BLE001 - a missing league is not a failure to add
            log.debug("Couldn't resolve the league for guild %s", interaction.guild_id,
                      exc_info=True)

        try:
            task = await weekly_store.add_process(
                self.bot,
                name=process,
                guild_id=interaction.guild_id,
                channel_id=channel.id,
                owner_id=interaction.user.id,
                weekday=day.value if day is not None else None,
                at=at,
                league_id=league_id,
            )
        except TaskError as exc:
            await interaction.followup.send(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface the reason to the admin
            log.exception("adding a weekly process failed")
            await interaction.followup.send(f"Couldn't add that: `{exc}`")
            return

        sport = weekly_store.sport_for_guild(self.bot, interaction.guild_id)
        chosen = (
            "you chose the day"
            if day is not None
            else f"this server plays {sport}"
        )
        await interaction.followup.send(
            f"Added **{process}** — {describe_schedule(task)}, reporting in "
            f"{channel.mention} ({chosen}).\n"
            f"It's **off**. Turn it on with `/weekly enable process:{process}`."
        )

    @group.command(name="remove", description="Take a process off the weekly run.")
    @app_commands.describe(process="Which process to remove")
    @is_admin()
    async def remove(self, interaction: discord.Interaction, process: str) -> None:
        await interaction.response.defer(ephemeral=True)
        task = await self._task_or_complain(interaction, process)
        if task is None:
            return
        try:
            await weekly_store.remove_process(self.bot, task)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the admin
            log.exception("removing a weekly process failed")
            await interaction.followup.send(f"Couldn't remove that: `{exc}`")
            return
        await interaction.followup.send(
            f"Removed **{process}** from the weekly run. It ran {task.fire_count} "
            f"time(s); `/weekly add` puts it back."
        )

    # --- the toggle ---------------------------------------------------------

    @group.command(name="enable", description="Let a process run on its schedule.")
    @app_commands.describe(process="Which process to switch on")
    @is_admin()
    async def enable(self, interaction: discord.Interaction, process: str) -> None:
        await self._toggle(interaction, process, enabled=True)

    @group.command(name="disable", description="Stop a process running on its schedule.")
    @app_commands.describe(process="Which process to switch off")
    @is_admin()
    async def disable(self, interaction: discord.Interaction, process: str) -> None:
        await self._toggle(interaction, process, enabled=False)

    async def _toggle(
        self, interaction: discord.Interaction, process: str, *, enabled: bool
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        task = await self._task_or_complain(interaction, process)
        if task is None:
            return
        if (task.status == STATUS_ACTIVE) == enabled:
            await interaction.followup.send(
                f"**{process}** is already {'on' if enabled else 'off'}."
            )
            return
        try:
            await weekly_store.set_enabled(self.bot, task, enabled)
        except TaskError as exc:
            await interaction.followup.send(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface the reason to the admin
            log.exception("toggling a weekly process failed")
            await interaction.followup.send(f"Couldn't do that: `{exc}`")
            return
        if enabled:
            stamp = (
                f"<t:{int(task.next_fire_at.timestamp())}:F>"
                if task.next_fire_at else "never"
            )
            await interaction.followup.send(
                f"**{process}** is on — {describe_schedule(task)}. Next run: {stamp}."
            )
        else:
            await interaction.followup.send(
                f"**{process}** is off. It keeps its schedule, and "
                f"`/weekly run process:{process}` still runs it by hand."
            )

    # --- running one now ----------------------------------------------------

    @group.command(name="run", description="Run a process now, off its schedule.")
    @app_commands.describe(process="Which process to run")
    @is_admin()
    async def run(self, interaction: discord.Interaction, process: str) -> None:
        await interaction.response.defer(ephemeral=True)
        task = await self._task_or_complain(interaction, process)
        if task is None:
            return
        # A long run can outlast the interaction: Discord gives a deferred
        # response fifteen minutes, and a process is allowed longer than that on
        # purpose (see PROCESS_TIMEOUT). The work still finishes and still posts
        # its report -- it is only this confirmation that may not land.
        try:
            outcome = await weekly_store.run_now(self.bot, task)
        except TaskError as exc:
            await interaction.followup.send(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface the reason to the admin
            log.exception("running a weekly process failed")
            await interaction.followup.send(f"**{process}** failed: `{exc}`")
            return

        # Forcing a run does not move next_fire_at: this is not the week's run,
        # and it should not take the week's run's place.
        if not outcome.announce:
            await interaction.followup.send(f"**{process}**: {outcome.body}")
            return
        try:
            await self._post(task, outcome)
        except Exception as exc:  # noqa: BLE001 - the work is done either way
            log.exception("posting a weekly report failed")
            await interaction.followup.send(
                f"**{process}** ran, but the report wouldn't post: `{exc}`\n\n"
                f"{outcome.body}"
            )
            return
        await interaction.followup.send(
            f"**{process}** ran; the report is in <#{task.channel_id}>."
        )

    # --- shared -------------------------------------------------------------

    async def _task_or_complain(
        self, interaction: discord.Interaction, process: str
    ) -> ScheduledTask | None:
        """This server's row for ``process``, or None having said why not."""
        try:
            task = await weekly_store.process_task(
                self.bot, interaction.guild_id, process
            )
        except TaskError as exc:
            await interaction.followup.send(str(exc))
            return None
        if task is None:
            await interaction.followup.send(
                f"This server doesn't run `{process}` weekly. "
                f"`/weekly add process:{process}` adds it."
            )
            return None
        return task

    @add.autocomplete("process")
    async def _addable(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Everything this bot offers, minus what the server already runs."""
        already: set[str] = set()
        try:
            already = set(
                await weekly_store.processes_for_guild(self.bot, interaction.guild_id)
            )
        except Exception:  # noqa: BLE001 - an autocomplete must never raise
            log.debug("weekly autocomplete couldn't read the server's processes",
                      exc_info=True)
        sport = weekly_store.sport_for_guild(self.bot, interaction.guild_id)
        return _choices(
            [
                process.name
                for process in self.bot.weekly.all()
                if process.name not in already and process.suits(sport)
            ],
            current,
        )

    @remove.autocomplete("process")
    @enable.autocomplete("process")
    @disable.autocomplete("process")
    @run.autocomplete("process")
    async def _added(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """What this server actually runs -- the only names these commands take."""
        try:
            names = list(
                await weekly_store.processes_for_guild(self.bot, interaction.guild_id)
            )
        except Exception:  # noqa: BLE001 - an autocomplete must never raise
            log.debug("weekly autocomplete couldn't read the server's processes",
                      exc_info=True)
            return []
        return _choices(names, current)


def _choices(names: list[str], current: str) -> list[app_commands.Choice[str]]:
    needle = current.strip().lower()
    return [
        app_commands.Choice(name=name, value=name)
        for name in sorted(names)
        if not needle or needle in name.lower()
    ][:25]


def _view_title(view: ProcessView, sport: str) -> str:
    if view.added:
        state = "on" if view.enabled else "off"
        return f"{'🟢' if view.enabled else '🔴'} {view.name} — {state}"
    if view.process is not None and not view.process.suits(sport):
        # Shown rather than hidden: "why isn't the max-PF report on the list?" is
        # a question worth answering once, in the list.
        return f"⚫ {view.name} — {view.process.sports_label}, not for a {sport} league"
    return f"⚪ {view.name} — not added"


def _view_body(view: ProcessView) -> str:
    lines = [view.summary or "(no description)"]
    task = view.task
    if task is None:
        lines.append(f"*`/weekly add process:{view.name}` to add it.*")
        return "\n".join(lines)

    lines.append(f"**When:** {describe_schedule(task)}")
    if task.channel_id:
        lines.append(f"**Reports in:** <#{task.channel_id}>")
    if view.enabled and task.next_fire_at is not None:
        lines.append(f"**Next:** <t:{int(task.next_fire_at.timestamp())}:F>")
    if task.last_fired_at is not None:
        lines.append(
            f"**Last run:** <t:{int(task.last_fired_at.timestamp())}:R> "
            f"({task.fire_count} in total)"
        )
    else:
        lines.append("**Last run:** never")
    if task.last_error:
        # The whole reason the last error is kept: a weekly job that stopped
        # producing anything looks exactly like one nobody switched on.
        lines.append(f"⚠️ last run: {task.last_error}")
    return "\n".join(lines)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Weekly(bot))
