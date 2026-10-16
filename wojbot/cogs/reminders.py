"""Set reminders: in a DM, in a channel, or across a server.

``/remind me`` is private, ``/remind channel`` is public, and ``/remind server``
is public and can tag other people. That last one is the only privileged
command here, and the reason is the mentions: making your own phone buzz on a
schedule is your business, and making everyone else's is the server's.

Like ``/rumor`` and unlike the bot's other groups, ``/remind`` is **not**
``guild_only`` -- a personal reminder is exactly the thing you want to set from a
DM, and ``/remind list`` in a DM is how you see the ones you set in servers you
have since stopped reading. The two commands that post into a server check for
one themselves rather than the whole group being gated.

Every reminder is stored (:mod:`wojbot.core.tasks`) and fired by the scheduler
cog through ``bot.tasks``, so they survive a restart -- which is most of the
point. What "friday 9am" *means* is :mod:`wojbot.core.schedule`, and the server's
``/setup timezone`` is what anchors it.

**Two permission checks matter here, and neither is the slash-command check.**
A member who can run ``/remind channel`` still may not point it at a channel
they cannot themselves write in -- otherwise the bot becomes a way to post into
a channel you have no access to. And a reminder outlives the permissions of
whoever set it, so the destination is re-checked at delivery: someone who has
since lost access to a channel does not keep a standing post in it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from ..core import tasks as task_store
from ..core.checks import VERIFIED_ROLES_KEY, has_privilege_in_guild, is_verified
from ..core.format import chunk
from ..core.schedule import (
    ACCEPTED_FORMS,
    UTC,
    ScheduleError,
    available_zones,
    parse_schedule,
)
from ..core.tasks import (
    KIND_REMINDER,
    LIVE_STATUSES,
    MAX_REMINDER_LENGTH,
    SCOPE_CHANNEL,
    SCOPE_SERVER,
    SCOPE_USER,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_PAUSED,
    ScheduledTask,
    TaskError,
)

log = logging.getLogger(__name__)

# Colour per scope, so how far a reminder reaches reads before the words do.
SCOPE_COLOURS = {
    SCOPE_USER: discord.Colour.blurple(),
    SCOPE_CHANNEL: discord.Colour.teal(),
    SCOPE_SERVER: discord.Colour.gold(),
}

SCOPE_LABELS = {
    SCOPE_USER: "DM",
    SCOPE_CHANNEL: "channel",
    SCOPE_SERVER: "server",
}

# Raw mention syntax, which is what Discord's own picker inserts into a text
# option. <@123> and <@!123> are users, <@&123> is a role.
_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")
# Anything left once the mentions are taken out. Used to tell "@everyone" and
# stray typing apart from a genuine list of people.
_MENTION_LEFTOVERS = re.compile(r"[\s,;]+")

WHEN_DESCRIBE = (
    "When — `in 90m`, `friday 8pm`, `every 3d`, `weekly on monday at 9am`. "
    "`/remind help` lists them all."
)


def schedule_help() -> str:
    """The accepted schedule phrases, as a bullet list."""
    return "\n".join(f"• {form}" for form in ACCEPTED_FORMS)


def reminder_embed(task: ScheduledTask, *, title: str) -> discord.Embed:
    """The confirmation shown when a reminder is set, changed, or listed."""
    embed = discord.Embed(
        title=title,
        description=task.text,
        colour=SCOPE_COLOURS.get(task.scope, discord.Colour.blurple()),
    )
    embed.add_field(name="When", value=task.describe_when(), inline=False)
    if task.next_fire_at is not None:
        stamp = int(task.next_fire_at.timestamp())
        embed.add_field(name="Next", value=f"<t:{stamp}:F> (<t:{stamp}:R>)", inline=False)
    where = SCOPE_LABELS.get(task.scope, task.scope)
    if task.channel_id:
        where = f"{where} · <#{task.channel_id}>"
    if task.mention_count:
        where = f"{where} · tagging {task.mention_count}"
    embed.add_field(name="Where", value=where, inline=False)
    if task.task_id is not None:
        embed.set_footer(text=f"#{task.task_id} · cancel it with /remind cancel")
    return embed


def render_mentions(task: ScheduledTask) -> str:
    """The mention prefix a firing leads with, or an empty string."""
    parts = [f"<@&{role_id}>" for role_id in task.mention_role_ids]
    parts += [f"<@{user_id}>" for user_id in task.mention_user_ids]
    return " ".join(parts)


def allowed_mentions_for(task: ScheduledTask) -> discord.AllowedMentions:
    """Permit exactly the pings this task was set up to make, and nothing else.

    Built from ids rather than left to the default. A reminder's text is written
    once and posted every week; if it happens to contain something Discord reads
    as a mention, an allow-all policy would have it pinging people the task was
    never given permission to ping.
    """
    users = [discord.Object(id=user_id) for user_id in task.mention_user_ids]
    # A channel reminder's one permitted ping is the person who set it, and only
    # if they asked for it.
    if task.scope == SCOPE_CHANNEL and task.payload.get("tag_owner"):
        users.append(discord.Object(id=task.owner_id))
    roles = [discord.Object(id=role_id) for role_id in task.mention_role_ids]
    return discord.AllowedMentions(
        everyone=False,
        users=users or False,
        roles=roles or False,
        replied_user=False,
    )


class Reminders(commands.Cog):
    """The ``/remind`` commands, and the handler that delivers them."""

    group = app_commands.Group(
        name="remind",
        description="Set a reminder for yourself, a channel, or the server.",
        # Not guild_only: setting and reviewing your own reminders from a DM is
        # half the point. See the module docstring.
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Registered against bot.tasks rather than the scheduler cog, which may
        # not have loaded yet -- see wojbot.core.task_runner.
        bot.tasks.register(KIND_REMINDER, self.deliver)

    # --- delivery ----------------------------------------------------------

    async def deliver(self, task: ScheduledTask) -> None:
        """Post one reminder. Raises on failure, which the runner records.

        Deliberately noisy about the reason: "I can't post in #general any more"
        is worth keeping on the row, because a recurring reminder that has gone
        quiet is otherwise indistinguishable from one nobody set.
        """
        if task.scope == SCOPE_USER:
            await self._deliver_dm(task)
            return
        await self._deliver_channel(task)

    async def _deliver_dm(self, task: ScheduledTask) -> None:
        user = self.bot.get_user(task.owner_id)
        if user is None:
            # Not in the cache -- the members intent is off, so this is the
            # normal path rather than the exceptional one.
            user = await self.bot.fetch_user(task.owner_id)
        embed = discord.Embed(
            title="⏰ Reminder",
            description=task.text,
            colour=SCOPE_COLOURS[SCOPE_USER],
        )
        if task.recurring:
            embed.set_footer(text=f"#{task.task_id} · {task.describe_when()}")
        await user.send(embed=embed)

    async def _deliver_channel(self, task: ScheduledTask) -> None:
        channel = self.bot.get_channel(task.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(task.channel_id)

        # Re-checked at delivery, not just when it was set: a reminder outlives
        # the access of whoever set it, and somebody who has since been removed
        # from a channel should not still have a standing post in it.
        member = await self._member_or_none(task.guild_id, task.owner_id)
        if member is None:
            raise TaskError(
                f"{task.owner_id} is no longer in this server, so their reminder "
                "wasn't posted."
            )
        if not channel.permissions_for(member).send_messages:
            raise TaskError(
                f"{member} can no longer post in #{channel.name}, so their "
                "reminder wasn't posted."
            )

        prefix = render_mentions(task)
        if not prefix and task.scope == SCOPE_CHANNEL and task.payload.get("tag_owner"):
            prefix = f"<@{task.owner_id}>"
        body = f"{prefix} ⏰ {task.text}" if prefix else f"⏰ {task.text}"
        await channel.send(body, allowed_mentions=allowed_mentions_for(task))

    async def _member_or_none(self, guild_id: int, user_id: int):
        """Fetch a member over HTTP, or None if they aren't there any more.

        Over HTTP because the bot runs without the members intent, so the member
        cache is mostly empty and a miss would read as "gone" -- the same reason
        :func:`wojbot.core.checks.has_privilege_in_guild` fetches.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None

    # --- shared command plumbing -------------------------------------------

    async def _build(
        self,
        interaction: discord.Interaction,
        *,
        scope: str,
        when: str,
        text: str,
        timezone: str | None,
        channel: discord.TextChannel | None = None,
        mention_users: tuple[int, ...] = (),
        mention_roles: tuple[int, ...] = (),
        tag_owner: bool = False,
    ) -> ScheduledTask:
        """Validate everything and return the task to store.

        Raises:
            TaskError or ScheduleError: with a message for the caller. Both are
                caught by the commands and shown as-is.
        """
        body = task_store.validate_reminder(text)
        guild_id = interaction.guild_id
        zone = timezone or task_store.resolve_timezone(self.bot, guild_id)
        now = datetime.now(UTC)
        schedule = parse_schedule(when, now=now, tz=zone)

        channel_id = channel.id if channel is not None else None
        task_store.validate_scope(scope, guild_id=guild_id, channel_id=channel_id)
        task_store.validate_mentions(scope, mention_users, mention_roles)

        payload = {"text": body}
        if tag_owner:
            payload["tag_owner"] = True

        return ScheduledTask(
            kind=KIND_REMINDER,
            scope=scope,
            owner_id=interaction.user.id,
            schedule=schedule,
            payload=payload,
            timezone=zone,
            guild_id=guild_id,
            channel_id=channel_id,
            mention_user_ids=mention_users,
            mention_role_ids=mention_roles,
            next_fire_at=schedule.next_fire_after(now),
        )

    async def _create(self, interaction: discord.Interaction, **kwargs) -> None:
        """Build, store, and confirm one reminder. The body of every set command."""
        try:
            task = await self._build(interaction, **kwargs)
            await task_store.record_task(self.bot, task)
        except (TaskError, ScheduleError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - surface the reason to the caller
            log.exception("setting a reminder failed")
            await interaction.followup.send(
                f"Couldn't set that: `{exc}`", ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=reminder_embed(task, title="Reminder set"), ephemeral=True
        )

    def _require_channel_access(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        """Both parties have to be able to post there. Raises :class:`TaskError`.

        The caller's own access is the check that matters for abuse: without it,
        ``/remind channel`` is a way to put a message into a channel you cannot
        see. The bot's own is checked at the same time only so the failure lands
        now rather than silently at the first firing.
        """
        mine = channel.permissions_for(interaction.guild.me)
        if not mine.send_messages:
            raise TaskError(f"I can't post in {channel.mention}.")
        theirs = channel.permissions_for(interaction.user)
        if not (theirs.view_channel and theirs.send_messages):
            raise TaskError(
                f"You can't post in {channel.mention}, so I won't do it on a timer "
                "for you either."
            )

    def _parse_mentions(
        self, interaction: discord.Interaction, raw: str | None
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Pull user and role ids out of what the mention picker inserted.

        Discord has no "list of mentionables" option type, so this is a string
        option that the client fills with real mention syntax as soon as the user
        picks somebody. Anything that isn't a mention is rejected rather than
        ignored: silently dropping a name means a reminder that was meant to tag
        four people tags three, every week, and nobody finds out.
        """
        if not raw or not raw.strip():
            return (), ()
        if "@everyone" in raw or "@here" in raw:
            raise TaskError(
                "Not @everyone or @here on a schedule — pick roles or people."
            )
        user_ids = tuple(dict.fromkeys(int(m) for m in _USER_MENTION.findall(raw)))
        role_ids = tuple(dict.fromkeys(int(m) for m in _ROLE_MENTION.findall(raw)))
        leftovers = _ROLE_MENTION.sub("", _USER_MENTION.sub("", raw))
        if _MENTION_LEFTOVERS.sub("", leftovers):
            raise TaskError(
                f"I couldn't read `{leftovers.strip()}` as somebody to tag. Type `@` "
                "and pick from the list so Discord fills the mention in."
            )
        if not user_ids and not role_ids:
            raise TaskError("No one to tag there. Type `@` and pick from the list.")

        # A role the bot cannot mention will silently not ping when it fires, so
        # say so now instead of every week from then on.
        for role_id in role_ids:
            role = interaction.guild.get_role(role_id)
            if role is None:
                raise TaskError(f"Role `{role_id}` isn't in this server.")
            if not role.mentionable and not interaction.guild.me.guild_permissions.mention_everyone:
                raise TaskError(
                    f"{role.name} isn't a mentionable role and I don't have "
                    '"Mention @everyone, @here, and All Roles", so tagging it '
                    "wouldn't actually ping anybody."
                )
        return user_ids, role_ids

    # --- setting -----------------------------------------------------------

    @group.command(name="me", description="Remind yourself, in a DM.")
    @app_commands.describe(
        when=WHEN_DESCRIBE,
        text="What to remind you about",
        timezone="IANA timezone for wall-clock times (default: the server's)",
    )
    async def remind_me(
        self,
        interaction: discord.Interaction,
        when: str,
        text: app_commands.Range[str, 1, MAX_REMINDER_LENGTH],
        timezone: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._create(
            interaction, scope=SCOPE_USER, when=when, text=text, timezone=timezone
        )

    @group.command(name="channel", description="Remind a channel, tagging you.")
    @app_commands.describe(
        when=WHEN_DESCRIBE,
        text="What the reminder says",
        channel="Where to post it (default: here)",
        tag_me="Tag you when it fires (default: yes)",
        timezone="IANA timezone for wall-clock times (default: the server's)",
    )
    async def remind_channel(
        self,
        interaction: discord.Interaction,
        when: str,
        text: app_commands.Range[str, 1, MAX_REMINDER_LENGTH],
        channel: discord.TextChannel | None = None,
        tag_me: bool = True,
        timezone: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(
                "That one needs a server. In a DM, `/remind me` is the one you want.",
                ephemeral=True,
            )
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.followup.send(
                "Pick a text channel to post in.", ephemeral=True
            )
            return
        try:
            self._require_channel_access(interaction, target)
        except TaskError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self._create(
            interaction,
            scope=SCOPE_CHANNEL,
            when=when,
            text=text,
            timezone=timezone,
            channel=target,
            tag_owner=tag_me,
        )

    @group.command(name="server", description="Remind a channel, tagging other people.")
    @app_commands.describe(
        when=WHEN_DESCRIBE,
        text="What the reminder says",
        mentions="Who to tag — type @ and pick people or roles",
        channel="Where to post it (default: here)",
        timezone="IANA timezone for wall-clock times (default: the server's)",
    )
    @is_verified()
    async def remind_server(
        self,
        interaction: discord.Interaction,
        when: str,
        text: app_commands.Range[str, 1, MAX_REMINDER_LENGTH],
        mentions: str,
        channel: discord.TextChannel | None = None,
        timezone: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(
                "That one needs a server — run it where the reminder should post.",
                ephemeral=True,
            )
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.followup.send(
                "Pick a text channel to post in.", ephemeral=True
            )
            return
        try:
            self._require_channel_access(interaction, target)
            mention_users, mention_roles = self._parse_mentions(interaction, mentions)
        except TaskError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self._create(
            interaction,
            scope=SCOPE_SERVER,
            when=when,
            text=text,
            timezone=timezone,
            channel=target,
            mention_users=mention_users,
            mention_roles=mention_roles,
        )

    # --- reviewing ---------------------------------------------------------

    @group.command(name="list", description="Show the reminders that are set.")
    async def list_reminders(self, interaction: discord.Interaction) -> None:
        """In a server, that server's. In a DM, your own, wherever you set them."""
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.guild_id is not None:
                found = await task_store.tasks_for_guild(self.bot, interaction.guild_id)
                header = f"**Reminders set in {interaction.guild.name}**"
                empty = (
                    "Nothing is set for this server. `/remind channel` sets one."
                )
            else:
                found = await task_store.tasks_for_owner(self.bot, interaction.user.id)
                header = "**Your reminders**"
                empty = "You haven't set any. `/remind me` sets one."
        except TaskError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - surface the reason to the caller
            log.exception("listing reminders failed")
            await interaction.followup.send(f"Couldn't read those: `{exc}`", ephemeral=True)
            return

        if not found:
            await interaction.followup.send(empty, ephemeral=True)
            return

        blocks = [_list_entry(task) for task in found]
        for index, message in enumerate(chunk([header, *blocks], sep="\n\n")):
            await interaction.followup.send(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                silent=index > 0,
            )

    @group.command(name="cancel", description="Cancel a reminder.")
    @app_commands.describe(reminder="Which reminder — its number from /remind list")
    async def cancel(self, interaction: discord.Interaction, reminder: int) -> None:
        await self._change_status(interaction, reminder, STATUS_CANCELLED, "Cancelled")

    @group.command(name="pause", description="Stop a reminder without cancelling it.")
    @app_commands.describe(reminder="Which reminder — its number from /remind list")
    async def pause(self, interaction: discord.Interaction, reminder: int) -> None:
        await self._change_status(interaction, reminder, STATUS_PAUSED, "Paused")

    @group.command(name="resume", description="Start a paused reminder again.")
    @app_commands.describe(reminder="Which reminder — its number from /remind list")
    async def resume(self, interaction: discord.Interaction, reminder: int) -> None:
        await self._change_status(interaction, reminder, STATUS_ACTIVE, "Resumed")

    async def _change_status(
        self, interaction: discord.Interaction, task_id: int, status: str, verb: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = await task_store.task_by_id(self.bot, task_id)
            if task is None:
                await interaction.followup.send(
                    f"There's no reminder `#{task_id}`.", ephemeral=True
                )
                return
            if not await self._may_manage(interaction, task):
                await interaction.followup.send(
                    f"Reminder `#{task_id}` isn't yours, and you're not an admin "
                    "in the server it posts to.",
                    ephemeral=True,
                )
                return
            if task.status not in LIVE_STATUSES:
                await interaction.followup.send(
                    f"Reminder `#{task_id}` is already {task.status}.", ephemeral=True
                )
                return
            if task.status == status:
                await interaction.followup.send(
                    f"Reminder `#{task_id}` is already {status}.", ephemeral=True
                )
                return
            await task_store.set_task_status(self.bot, task, status)
        except TaskError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - surface the reason to the caller
            log.exception("changing a reminder's status failed")
            await interaction.followup.send(f"Couldn't do that: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(
            embed=reminder_embed(task, title=f"{verb} reminder"), ephemeral=True
        )

    async def _may_manage(
        self, interaction: discord.Interaction, task: ScheduledTask
    ) -> bool:
        """Whoever set it, or anyone Verified in the server it posts to.

        The second half is what stops a reminder becoming permanent when the
        person who set it leaves: somebody has to be able to turn off a weekly
        post whose author is long gone.
        """
        if task.owner_id == interaction.user.id:
            return True
        if task.guild_id is None:
            return False
        return await has_privilege_in_guild(
            self.bot, task.guild_id, interaction.user, VERIFIED_ROLES_KEY
        )

    # --- autocomplete ------------------------------------------------------

    @remind_me.autocomplete("timezone")
    @remind_channel.autocomplete("timezone")
    @remind_server.autocomplete("timezone")
    async def _timezone_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return timezone_choices(current)

    @cancel.autocomplete("reminder")
    @pause.autocomplete("reminder")
    @resume.autocomplete("reminder")
    async def _reminder_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """The reminders the caller could plausibly be naming.

        Silent on failure -- an autocomplete that raises just shows nothing, and
        the command itself gives a proper answer either way.
        """
        try:
            found = await task_store.tasks_for_owner(self.bot, interaction.user.id)
            if interaction.guild_id is not None:
                # Plus anything else in this server, which an admin may manage.
                # Deduplicated on id, since their own guild tasks are in both.
                by_id = {task.task_id: task for task in found}
                for task in await task_store.tasks_for_guild(self.bot, interaction.guild_id):
                    by_id.setdefault(task.task_id, task)
                found = list(by_id.values())
        except Exception:  # noqa: BLE001 - an autocomplete must never raise at the user
            log.debug("reminder autocomplete failed", exc_info=True)
            return []
        needle = current.strip().lower()
        return [
            app_commands.Choice(
                name=f"#{task.task_id} · {_truncate(task.text, 70)}",
                value=task.task_id,
            )
            for task in found
            if not needle
            or needle in str(task.task_id)
            or needle in task.text.lower()
        ][:25]

    @group.command(name="help", description="What you can type for 'when'.")
    async def help_when(self, interaction: discord.Interaction) -> None:
        zone = task_store.resolve_timezone(self.bot, interaction.guild_id)
        embed = discord.Embed(
            title="Scheduling a reminder",
            description=schedule_help(),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Timezone",
            value=(
                f"Wall-clock times are read in **{zone}**"
                + (
                    " — a server admin can change that with `/setup timezone`."
                    if interaction.guild_id is not None
                    else ", unless you pass `timezone:`."
                )
            ),
            inline=False,
        )
        embed.add_field(
            name="Scopes",
            value=(
                "`/remind me` — a DM, just you.\n"
                "`/remind channel` — posts in a channel and tags you.\n"
                "`/remind server` — posts in a channel and tags other people. "
                "Admin only, because it's the one that makes somebody else's "
                "phone buzz."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


def timezone_choices(current: str) -> list[app_commands.Choice[str]]:
    """Up to 25 IANA zones matching ``current``.

    Shared with ``/setup timezone`` so both fields offer the same list. Matches
    anywhere in the name, so "york" finds America/New_York -- nobody remembers
    which continent Discord's autocomplete wants first.
    """
    needle = current.strip().lower().replace(" ", "_")
    zones = available_zones()
    if needle:
        starts = [zone for zone in zones if zone.lower().startswith(needle)]
        contains = [
            zone for zone in zones
            if needle in zone.lower() and not zone.lower().startswith(needle)
        ]
        zones = starts + contains
    return [app_commands.Choice(name=zone, value=zone) for zone in zones[:25]]


def _truncate(text: str, limit: int) -> str:
    """Fit text into a Discord component label."""
    single_line = " ".join(text.split())
    return (
        single_line if len(single_line) <= limit else single_line[: limit - 1] + "…"
    )


def _list_entry(task: ScheduledTask) -> str:
    """One reminder, as a block in ``/remind list``."""
    bits = [f"`#{task.task_id}`", f"**{SCOPE_LABELS.get(task.scope, task.scope)}**"]
    if task.channel_id:
        bits.append(f"<#{task.channel_id}>")
    bits.append(f"set by <@{task.owner_id}>")
    if task.status != STATUS_ACTIVE:
        bits.append(f"— *{task.status}*")
    head = " · ".join(bits)

    when = task.describe_when()
    if task.next_fire_at is not None:
        when += f" — next <t:{int(task.next_fire_at.timestamp())}:R>"
    lines = [head, f"> {_truncate(task.text, 200)}", f"*{when}*"]
    if task.mention_count:
        lines.append(f"*tags {task.mention_count}*")
    if task.last_error:
        # A recurring reminder that has been failing looks identical to one
        # nobody set until this is shown.
        lines.append(f"⚠️ last firing: {task.last_error}")
    return "\n".join(lines)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reminders(bot))
