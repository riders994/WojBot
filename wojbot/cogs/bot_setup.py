"""Bot setup cog: the front door for configuring the bot.

Three scopes, three audiences:

* ``/setup global`` — bot-wide settings, owner only. These are *defaults* for
  servers that have never chosen: a server that has set its own value keeps it,
  so moving a global never silently changes one that already decided (see
  ``INHERITED_FROM_BOT`` in :mod:`wojbot.core.defaults`).
* ``/setup show`` — this server's settings and what is still outstanding,
  for anyone with admin access.
* ``/setup wizard`` — walks a new server through the same settings with
  buttons, then hands off to the league commands.

Every setting has a server-side toggle here, and a bot-wide one under ``global``
where it makes sense to have a fleet default. Writing settings is what this cog
is for, so the gating stays in one place: as its own command in the messaging
cog, the dad-joke toggle was the one setting any member could change.

Two settings are still set elsewhere, because they are more than a toggle:
privileged roles by ``/verify``, which adds and removes one role at a time, and
the league binding by ``/commish load``, which loads the league as well as names
it. Both are reported by ``/setup show`` and set by the wizard.

Not every server that invites the bot runs a league, and the rumor settings are
the ones that only make sense when it does — see :func:`is_league_server`. The
wizard skips that step, ``/setup show`` says so rather than listing a channel
nothing would ever post to, and ``/setup rumorchannel`` declines.

An inherited setting's toggle has three states, not two — on, off, and following
the bot-wide default. Without the third, the first use of the toggle would
detach a server from the default permanently.

Privileged roles are matched by name and stored per guild, never globally: two
servers can both have a role called "Commish" without either granting access in
the other.
"""

from __future__ import annotations

import logging

import discord
import yaml
from discord import app_commands
from discord.ext import commands

from ..core.checks import (
    TIER_LABELS,
    TIER_LADDER,
    is_bot_owner,
    is_verified,
)
from ..core.elo import (
    ELO_SYS_CONFIG,
    GUILD_LEAGUE_KEY,
    build_system,
    configured_league,
    runtime_key,
)
from ..core.defaults import DEFAULT_CHANNEL_KEY
from ..core.restart import RESTART_NOTICE_KEY
from ..core.rumor import RUMOR_CHANNEL_KEY, league_for_guild

log = logging.getLogger(__name__)

DAD_JOKE_KEY = "dad_joke"
WIZARD_TIMEOUT = 300.0

# The tiers the setup wizard offers, highest first -- the positive ladder only.
# Restricted is deliberately not here: this flow is about granting a new server's
# roles, and a wizard that asked who to shut out during onboarding would be
# asking the wrong question. It is managed from /verify instead.
TIERS = tuple((key, TIER_LABELS[key]) for key in TIER_LADDER)

# Every inherited setting's server-side toggle offers these three: the two
# values, plus giving the choice back so the bot-wide default applies again.
ON, OFF, INHERIT = "on", "off", "inherit"
STATE_CHOICES = [
    app_commands.Choice(name="On", value=ON),
    app_commands.Choice(name="Off", value=OFF),
    app_commands.Choice(name="Follow the bot default", value=INHERIT),
]

# A setting a server owns outright gets the two values and no third: there is no
# bot-wide default for it to fall back to, and offering "follow the default"
# where none exists would be a choice that does nothing.
TOGGLE_CHOICES = STATE_CHOICES[:2]


def describe_setting(configs, guild_id: int, key: str) -> str:
    """``on (this server's own)`` — the value and where it came from."""
    state = "on" if configs.get_guild(guild_id)[key] else "off"
    source = "this server's own" if configs.guild_has_own(guild_id, key) \
        else "following the bot default"
    return f"{state} ({source})"


def configured_leagues() -> list[str]:
    """League keys from sys_config, or an empty list if it cannot be read."""
    try:
        config = yaml.safe_load(ELO_SYS_CONFIG.read_text()) or {}
    except (OSError, yaml.YAMLError):
        log.warning("Could not read %s", ELO_SYS_CONFIG, exc_info=True)
        return []
    return sorted(config.get("ratings_configs", {}))


async def is_league_server(bot, guild_id: int) -> bool:
    """Whether this server runs a league, and so has rumors worth setting up.

    A server becomes one two ways, and either counts:

    * bot-side, by ``/commish load`` (or the wizard's league step), which names
      the Elo league this server drives;
    * warehouse-side, by ``/commish db link``, which writes this server onto the
      league's ``dim_league`` row. ``/commish db migrate`` does both.

    ``/rumor report`` keys off the warehouse row, so that is the binding that
    truly decides — but a server bound only bot-side is a league mid-onboarding
    rather than a stranger, and the config binding is free to read, so it is
    taken first and the query only runs when it comes up empty.

    A server nobody can vouch for is treated as not a league, the database being
    down included. That is the safe way round: the rumor settings reappear the
    moment the binding does, and :meth:`BotSetup.outstanding` reports a dead
    database alongside, so the omission never has to be guessed at.
    """
    if configured_league(bot, guild_id) is not None:
        return True
    sql = getattr(bot, "sql", None)
    if sql is None or sql.connection is None:
        return False
    try:
        return (await league_for_guild(bot, guild_id)) is not None
    except Exception:  # noqa: BLE001 - setup must still render with the DB sick
        log.warning("Could not check the league binding for %s", guild_id, exc_info=True)
        return False


def describe_channel(configs, guild: discord.Guild, key: str) -> str:
    """``#woj-wire`` — the configured channel, or why it isn't usable.

    A stored id can outlive the channel it named, and the result reads the same
    as "never set" unless the difference is spelled out.
    """
    channel_id = configs.get_guild(guild.id).get(key)
    if channel_id is None:
        return "not set"
    channel = guild.get_channel(channel_id)
    return channel.mention if channel else f"`{channel_id}` (no such channel here)"


def _role_status(guild: discord.Guild, names) -> tuple[list[str], list[str]]:
    """Split configured role names into those the guild has and those it lacks.

    Roles are configured by name, so a name that matches nothing grants nobody
    anything — a silent no-op worth surfacing, since the usual cause is a role
    renamed in Discord after it was configured here.
    """
    live = {role.name for role in guild.roles}
    return [n for n in names if n in live], [n for n in names if n not in live]


class SetupWizard(discord.ui.View):
    """Roles, league, rumor channel and dad jokes over one message.

    Each step rebuilds the view's children rather than sending a new message,
    so the whole thing stays a single ephemeral reply the admin can dismiss.

    Built through :meth:`create` rather than the constructor, because working
    out whether this server is a league can take a query and a view cannot be
    built asynchronously.
    """

    def __init__(
        self, cog: "BotSetup", interaction: discord.Interaction, *, is_league: bool
    ) -> None:
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.cog = cog
        self.bot = cog.bot
        self.guild = interaction.guild
        self.user = interaction.user
        self.is_league = is_league
        self.step = 0
        self.done: list[str] = []
        self._render()

    @classmethod
    async def create(
        cls, cog: "BotSetup", interaction: discord.Interaction
    ) -> "SetupWizard":
        """Resolve what this server is, then build the first step."""
        return cls(
            cog,
            interaction,
            is_league=await is_league_server(cog.bot, interaction.guild_id),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # The reply is ephemeral, so only the invoker can see it -- but the
        # check keeps a handed-off component from being driven by anyone else.
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This wizard belongs to whoever started it.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        self.clear_items()

    # --- rendering ---------------------------------------------------------

    def _steps(self) -> list[tuple[str, object, object]]:
        """``(title, build, body)`` for each step of this run.

        The rumor step is only there for a league server. Elsewhere it would ask
        for a channel that nothing could ever post to, and leave behind a setting
        that reads like a working one. Binding a league at the step before adds
        it, which is why the list is rebuilt on every render rather than fixed
        when the wizard starts.
        """
        steps: list[tuple[str, object, object]] = [
            ("Privileged roles", self._step_roles, self._body_roles),
            ("League", self._step_league, self._body_league),
        ]
        if self.is_league:
            steps.append(("Rumor channel", self._step_rumors, self._body_rumors))
        steps.append(("Dad jokes", self._step_jokes, self._body_jokes))
        steps.append(("Setup complete", self._step_done, self._body_done))
        return steps

    def _render(self) -> None:
        self.clear_items()
        steps = self._steps()
        # Clamped because the step list can grow underneath the index: a league
        # bound at step two inserts the rumor step after it.
        self.step = max(0, min(self.step, len(steps) - 1))
        steps[self.step][1]()

    def _embed(self) -> discord.Embed:
        steps = self._steps()
        last = len(steps) - 1
        embed = discord.Embed(
            title=f"WojBot setup — {steps[self.step][0]}",
            description=steps[self.step][2](),
            colour=discord.Colour.blurple(),
        )
        if self.step < last:
            embed.set_footer(text=f"Step {self.step + 1} of {last}")
        return embed

    def _body_roles(self) -> str:
        config = self.bot.configs.get_guild(self.guild.id)
        lines = []
        for key, label in TIERS:
            have, missing = _role_status(self.guild, config[key])
            shown = ", ".join(f"`{n}`" for n in have) or "(none)"
            gone = f" · missing here: {', '.join(f'`{n}`' for n in missing)}" if missing else ""
            lines.append(f"**{label}:** {shown}{gone}")
        return (
            "Pick the roles that grant each tier in **this server**. Choosing "
            "replaces that tier's list.\nServer administrators and the bot "
            "owner always pass regardless.\n\n" + "\n".join(lines)
        )

    def _body_league(self) -> str:
        bound = configured_league(self.bot, self.guild.id)
        current = f"Currently bound to `{bound}`." if bound else "Not bound to a league yet."
        leagues = configured_leagues()
        if not leagues:
            return f"{current}\n\nNo leagues are configured in `sys_config.yml`."
        note = "" if self.is_league else (
            "\n\nThe league commands and the rumor mill only work once this "
            "server drives a league; skip it if this one doesn't."
        )
        return f"{current}\n\nPick the Elo league this server drives.{note}"

    def _body_rumors(self) -> str:
        current = describe_channel(self.bot.configs, self.guild, RUMOR_CHANNEL_KEY)
        return (
            f"Rumors post to {current}.\n\nPick the channel `/rumor report` "
            "announces to. Without one, rumors are still recorded — they just "
            "go out to nobody."
        )

    def _body_jokes(self) -> str:
        current = describe_setting(self.bot.configs, self.guild.id, DAD_JOKE_KEY)
        return (
            f"Dad jokes are **{current}**.\n\nOn or off makes it this server's "
            "own, so a later change to the bot-wide default leaves it alone. "
            "*Follow default* hands the choice back."
        )

    def _body_done(self) -> str:
        body = "\n".join(f"• {line}" for line in self.done) or "• (nothing changed)"
        outstanding = self.cog.outstanding(self.guild, is_league=self.is_league)
        return f"**Done**\n{body}\n\n{outstanding}"

    async def _advance(self, interaction: discord.Interaction) -> None:
        self.step += 1
        self._render()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    # --- step: roles -------------------------------------------------------

    def _step_roles(self) -> None:
        for key, label in TIERS:
            select = discord.ui.RoleSelect(
                placeholder=f"{label} roles", min_values=0, max_values=25,
            )

            async def callback(interaction: discord.Interaction, key=key, label=label,
                               select=select):
                names = [role.name for role in select.values]
                self.bot.configs.set_guild(self.guild.id, {key: names})
                self.done.append(
                    f"{label} roles set to {', '.join(f'`{n}`' for n in names) or '(none)'}"
                )
                self._render()
                await interaction.response.edit_message(embed=self._embed(), view=self)

            select.callback = callback
            self.add_item(select)
        self.add_item(_NextButton(self, "Next ›"))

    # --- step: league ------------------------------------------------------

    def _step_league(self) -> None:
        leagues = configured_leagues()
        if leagues:
            bound = configured_league(self.bot, self.guild.id)
            select = discord.ui.Select(
                placeholder="Elo league for this server",
                options=[
                    discord.SelectOption(label=key, value=key, default=(key == bound))
                    for key in leagues[:25]
                ],
            )

            async def callback(interaction: discord.Interaction, select=select):
                key = select.values[0]
                await interaction.response.defer()
                try:
                    # Same path /commish load takes, so the league is actually
                    # loaded rather than merely named in the config.
                    system = await build_system(self.bot, key)
                except Exception as exc:  # noqa: BLE001 - surface it in the wizard
                    log.exception("wizard: league load failed")
                    self.done.append(f"League `{key}` could not be loaded: `{exc}`")
                else:
                    self.bot.runtime.set(runtime_key(system.league), system)
                    self.bot.configs.set_guild(
                        self.guild.id, {GUILD_LEAGUE_KEY: system.league}
                    )
                    # This server runs a league as of now, so the rumor step
                    # joins the wizard ahead of the one being stood on.
                    self.is_league = True
                    self.done.append(f"League bound to `{system.league}`")
                self._render()
                await interaction.edit_original_response(embed=self._embed(), view=self)

            select.callback = callback
            self.add_item(select)
        self.add_item(_NextButton(self, "Next ›"))

    # --- step: rumor channel (league servers only) -------------------------

    def _step_rumors(self) -> None:
        select = discord.ui.ChannelSelect(
            placeholder="Channel for reported rumors",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
        )

        async def callback(interaction: discord.Interaction, select=select):
            if not select.values:
                self._render()
                await interaction.response.edit_message(embed=self._embed(), view=self)
                return
            channel = select.values[0]
            self.bot.configs.set_guild(self.guild.id, {RUMOR_CHANNEL_KEY: channel.id})
            self.done.append(f"Rumors will post to {channel.mention}")
            self._render()
            await interaction.response.edit_message(embed=self._embed(), view=self)

        select.callback = callback
        self.add_item(select)
        self.add_item(_NextButton(self, "Next ›"))

    # --- step: dad jokes ---------------------------------------------------

    def _step_jokes(self) -> None:
        for label, value, style in (
            ("On", ON, discord.ButtonStyle.success),
            ("Off", OFF, discord.ButtonStyle.secondary),
            ("Follow default", INHERIT, discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=label, style=style)

            async def callback(interaction: discord.Interaction, value=value):
                if value == INHERIT:
                    self.bot.configs.clear_guild(self.guild.id, DAD_JOKE_KEY)
                    self.done.append("Dad jokes set to follow the bot default")
                else:
                    self.bot.configs.set_guild(
                        self.guild.id, {DAD_JOKE_KEY: value == ON}
                    )
                    self.done.append(f"Dad jokes turned {value}")
                self._render()
                await interaction.response.edit_message(embed=self._embed(), view=self)

            button.callback = callback
            self.add_item(button)
        self.add_item(_NextButton(self, "Finish ›"))

    def _step_done(self) -> None:
        self.stop()


class _NextButton(discord.ui.Button):
    def __init__(self, view: SetupWizard, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self._wizard = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._wizard._advance(interaction)


class BotSetup(commands.Cog):
    """Global and per-server setup, plus the guided walkthrough."""

    group = app_commands.Group(
        name="setup",
        description="Configure the bot for this server.",
        guild_only=True,
    )
    # Nested subgroup: /setup global <...> — bot-wide, owner only.
    globals_group = app_commands.Group(
        name="global",
        description="Bot-wide settings (owner only).",
        parent=group,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def outstanding(self, guild: discord.Guild, *, is_league: bool) -> str:
        """What still needs doing for this server, with the command for each.

        Config-only checks: everything here is a dict or file read, so the
        overview stays instant and works with the database down. Whether this
        server is a league is the one thing that can take a query, so it is
        resolved by the caller (:func:`is_league_server`) and passed in.
        """
        steps: list[str] = []
        config = self.bot.configs.get_guild(guild.id)
        for key, label in TIERS:
            have, missing = _role_status(guild, config[key])
            if not have:
                steps.append(
                    f"No {label.lower()} role exists in this server"
                    + (f" (configured: {', '.join(missing)})" if missing else "")
                    + " — `/verify add`"
                )
        league = configured_league(self.bot, guild.id)
        if league is None:
            steps.append(
                "No league bound to this server — `/setup wizard` or `/commish load`"
            )
        else:
            system = self.bot.runtime.get(runtime_key(league))
            if system is None:
                steps.append(f"League `{league}` is bound but not loaded — `/commish load`")
            else:
                elo_league = system.elo_league
                seasons = getattr(elo_league, "seasons", {}) or {}
                if not seasons:
                    steps.append("The league has no seasons — `/commish season add`")
                unpinned = [y for y, s in seasons.items() if s.get("platform") is None]
                if unpinned:
                    steps.append(
                        f"{len(unpinned)} season(s) name no platform — "
                        "`/commish season platform`"
                    )
                if not any(s.get("league_members") for s in seasons.values()):
                    steps.append("No rosters scraped yet — `/commish sync`")
        # Rumors belong to a league. A server that runs none has nothing to
        # report, so a missing rumor channel isn't something left undone.
        if is_league:
            channel_id = config.get(RUMOR_CHANNEL_KEY)
            if channel_id is None:
                steps.append("No rumor channel set — `/setup rumorchannel`")
            elif guild.get_channel(channel_id) is None:
                steps.append(
                    f"The rumor channel (`{channel_id}`) no longer exists — "
                    "`/setup rumorchannel`"
                )
        # Only worth raising for a server that asked for notices: a default
        # channel is optional until something wants to use it.
        if config[RESTART_NOTICE_KEY]:
            channel_id = config.get(DEFAULT_CHANNEL_KEY)
            if channel_id is None:
                steps.append(
                    "Restart notices are on but there's no default channel — "
                    "`/setup defaultchannel`"
                )
            elif guild.get_channel(channel_id) is None:
                steps.append(
                    f"The default channel (`{channel_id}`) no longer exists — "
                    "`/setup defaultchannel`"
                )
        sql = getattr(self.bot, "sql", None)
        if sql is None:
            steps.append("No database service — check `SQL_CONN_URI`")
        elif sql.connection is None:
            # The cog loads even when the connect fails, so a live service with
            # no connection is the normal shape of "the database is down".
            steps.append("Database not connected — `/sql reconnect`")
        if not steps:
            return "**Outstanding:** nothing — this server is set up."
        return "**Outstanding**\n" + "\n".join(f"• {s}" for s in steps)

    @group.command(name="show", description="Show this server's settings and what's left to do.")
    @is_verified()
    async def show(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        # Deferred for the one check that can reach the database; everything
        # below it is config.
        await interaction.response.defer(ephemeral=True)
        is_league = await is_league_server(self.bot, guild.id)
        config = self.bot.configs.get_guild(guild.id)
        lines = []
        for key, label in TIERS:
            have, missing = _role_status(guild, config[key])
            shown = ", ".join(f"`{n}`" for n in have) or "(none)"
            gone = f" · not in this server: {', '.join(f'`{n}`' for n in missing)}" if missing else ""
            lines.append(f"**{label} roles:** {shown}{gone}")
        league = configured_league(self.bot, guild.id)
        lines.append(f"**League:** `{league}`" if league else "**League:** not bound")
        if is_league:
            lines.append(
                f"**Rumor channel:** "
                f"{describe_channel(self.bot.configs, guild, RUMOR_CHANNEL_KEY)}"
            )
        else:
            # Said rather than left out: a setting that quietly isn't listed
            # reads as one the bot forgot.
            lines.append(
                "**Rumor channel:** n/a — this server doesn't run a league"
            )
        lines.append(
            f"**Default channel:** "
            f"{describe_channel(self.bot.configs, guild, DEFAULT_CHANNEL_KEY)}"
        )
        lines.append(
            f"**Restart notices:** "
            f"{'on' if config[RESTART_NOTICE_KEY] else 'off'}"
        )
        lines.append(
            f"**Dad jokes:** {describe_setting(self.bot.configs, guild.id, DAD_JOKE_KEY)}"
        )

        embed = discord.Embed(
            title=f"WojBot setup — {guild.name}",
            description=(
                "\n".join(lines) + "\n\n" + self.outstanding(guild, is_league=is_league)
            ),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(
            text="Roles: /verify · dad jokes: /dadjokes · league: /commish · "
                 "rumors: /setup rumorchannel · notices: /setup restartnotices · "
                 "or walk through it with /setup wizard"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(
        name="rumorchannel", description="Set the channel reported rumors post to."
    )
    @app_commands.describe(channel="Where /rumor report announces to")
    @is_verified()
    async def rumorchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await is_league_server(self.bot, interaction.guild_id):
            # Storing it would look like it worked, and nothing would ever post
            # there: /rumor report reaches a server through its league.
            await interaction.followup.send(
                "This server doesn't run a league, so there are no rumors to post "
                "in it. Bind one first with `/setup wizard` or `/commish load`, "
                "and `/commish db migrate` to adopt its database row — then set "
                "the channel.",
                ephemeral=True,
            )
            return
        self.bot.configs.set_guild(interaction.guild_id, {RUMOR_CHANNEL_KEY: channel.id})
        # Say up front if the bot can't actually post there: the setting saves
        # either way, and the failure would otherwise only surface later, to
        # whoever reported the first rumor.
        allowed = channel.permissions_for(interaction.guild.me).send_messages
        warning = (
            "" if allowed
            else f"\n\n⚠️ I can't send messages in {channel.mention} — "
                 "rumors will be recorded but not announced until that's fixed."
        )
        await interaction.followup.send(
            f"Reported rumors will post to {channel.mention}.{warning}", ephemeral=True
        )

    @group.command(
        name="defaultchannel",
        description="Set the channel the bot speaks to this server in.",
    )
    @app_commands.describe(channel="Where the bot posts notices meant for this server")
    @is_verified()
    async def defaultchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        self.bot.configs.set_guild(interaction.guild_id, {DEFAULT_CHANNEL_KEY: channel.id})
        # Same warning as the rumor channel, for the same reason: the setting
        # saves either way, and a channel the bot can't post in would otherwise
        # only reveal itself the next time it had something to say.
        allowed = channel.permissions_for(interaction.guild.me).send_messages
        warning = (
            "" if allowed
            else f"\n\n⚠️ I can't send messages in {channel.mention} — nothing "
                 "will reach it until that's fixed."
        )
        config = self.bot.configs.get_guild(interaction.guild_id)
        subscribed = (
            "" if config[RESTART_NOTICE_KEY]
            else "\n\nNothing posts here yet. `/setup restartnotices state:On` "
                 "is the one thing that currently uses it."
        )
        await interaction.response.send_message(
            f"I'll speak to this server in {channel.mention}.{warning}{subscribed}",
            ephemeral=True,
        )

    @group.command(
        name="restartnotices",
        description="Say whether this server hears when the bot restarts.",
    )
    @app_commands.describe(state="On to subscribe this server, off to stop")
    @app_commands.choices(state=TOGGLE_CHOICES)
    @is_verified()
    async def restartnotices(
        self, interaction: discord.Interaction, state: app_commands.Choice[str]
    ) -> None:
        guild_id = interaction.guild_id
        enabled = state.value == ON
        self.bot.configs.set_guild(guild_id, {RESTART_NOTICE_KEY: enabled})
        if not enabled:
            await interaction.response.send_message(
                "This server won't hear about restarts any more.", ephemeral=True
            )
            return

        # Subscribing without somewhere to post is the one way to turn this on
        # and get nothing, so it is answered here rather than left to be
        # noticed after the next restart didn't say anything.
        channel_id = self.bot.configs.get_guild(guild_id).get(DEFAULT_CHANNEL_KEY)
        if channel_id is None:
            await interaction.response.send_message(
                "This server is subscribed — but there's nowhere to post yet. "
                "Name a channel with `/setup defaultchannel` and the next "
                "restart will say so there.",
                ephemeral=True,
            )
            return
        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            await interaction.response.send_message(
                f"This server is subscribed — but the default channel "
                f"(`{channel_id}`) no longer exists. Point `/setup "
                "defaultchannel` at a new one.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"This server will hear about restarts in {channel.mention} — "
            "both the ones I'm asked for and the ones I'm not.",
            ephemeral=True,
        )

    @group.command(name="dadjokes", description="Turn dad jokes on or off for this server.")
    @app_commands.describe(state="On, off, or follow whatever the bot-wide default is")
    @app_commands.choices(state=STATE_CHOICES)
    @is_verified()
    async def dadjokes(
        self, interaction: discord.Interaction, state: app_commands.Choice[str]
    ) -> None:
        guild_id = interaction.guild_id
        if state.value == INHERIT:
            self.bot.configs.clear_guild(guild_id, DAD_JOKE_KEY)
            now = "on" if self.bot.configs.get_guild(guild_id)[DAD_JOKE_KEY] else "off"
            await interaction.response.send_message(
                f"Dad jokes now follow the bot-wide default, currently **{now}**.",
                ephemeral=True,
            )
            return
        enabled = state.value == ON
        self.bot.configs.set_guild(guild_id, {DAD_JOKE_KEY: enabled})
        await interaction.response.send_message(
            f"Dad jokes are now **{'on' if enabled else 'off'}** for this server, "
            "and stay that way whatever the bot-wide default does.",
            ephemeral=True,
        )

    @group.command(name="wizard", description="Walk through setting this server up.")
    @is_verified()
    async def wizard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        view = await SetupWizard.create(self, interaction)
        await interaction.followup.send(
            embed=view._embed(), view=view, ephemeral=True
        )

    # --- /setup global: bot-wide, owner only -------------------------------

    @globals_group.command(name="show", description="Show the bot-wide settings.")
    @is_bot_owner()
    async def global_show(self, interaction: discord.Interaction) -> None:
        config = self.bot.configs.get_bot()
        state = "on" if config[DAD_JOKE_KEY] else "off"
        own = [
            guild for guild in self.bot.guilds
            if self.bot.configs.guild_has_own(guild.id, DAD_JOKE_KEY)
        ]
        bound = sum(
            1 for guild in self.bot.guilds
            if configured_league(self.bot, guild.id) is not None
        )
        embed = discord.Embed(
            title="WojBot — global settings",
            description=(
                f"**Dad jokes (default):** {state}\n\n"
                f"**Servers:** {len(self.bot.guilds)} — {len(own)} have set dad jokes "
                f"themselves and keep their own, {bound} are bound to a league\n"
                f"**Configured leagues:** "
                + (", ".join(f"`{k}`" for k in configured_leagues()) or "(none)")
            ),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="A default for servers that never chose — owner only.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @globals_group.command(
        name="dadjokes", description="Set the dad-joke default for servers that never chose."
    )
    @app_commands.describe(enabled="True to default them on, False to default them off")
    @is_bot_owner()
    async def global_dadjokes(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        self.bot.configs.set_bot({DAD_JOKE_KEY: enabled})
        kept = sum(
            1 for guild in self.bot.guilds
            if self.bot.configs.guild_has_own(guild.id, DAD_JOKE_KEY)
        )
        note = (
            f" {kept} server(s) have set their own and are unaffected." if kept else ""
        )
        await interaction.response.send_message(
            f"Dad jokes now default to **{'on' if enabled else 'off'}**.{note}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BotSetup(bot))
