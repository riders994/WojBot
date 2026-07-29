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
    ADMIN_ROLES_KEY,
    COMMISSIONER_ROLES_KEY,
    is_bot_owner,
    is_privileged,
)
from ..core.elo import (
    ELO_SYS_CONFIG,
    GUILD_LEAGUE_KEY,
    build_system,
    configured_league,
    runtime_key,
)
from ..core.rumor import RUMOR_CHANNEL_KEY

log = logging.getLogger(__name__)

DAD_JOKE_KEY = "dad_joke"
WIZARD_TIMEOUT = 300.0

TIERS = ((COMMISSIONER_ROLES_KEY, "Commissioner"), (ADMIN_ROLES_KEY, "Admin"))

# Every inherited setting's server-side toggle offers these three: the two
# values, plus giving the choice back so the bot-wide default applies again.
ON, OFF, INHERIT = "on", "off", "inherit"
STATE_CHOICES = [
    app_commands.Choice(name="On", value=ON),
    app_commands.Choice(name="Off", value=OFF),
    app_commands.Choice(name="Follow the bot default", value=INHERIT),
]


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
    """Four steps over one message: roles, league, rumor channel, dad jokes.

    Each step rebuilds the view's children rather than sending a new message,
    so the whole thing stays a single ephemeral reply the admin can dismiss.
    """

    def __init__(self, cog: "BotSetup", interaction: discord.Interaction) -> None:
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.cog = cog
        self.bot = cog.bot
        self.guild = interaction.guild
        self.user = interaction.user
        self.step = 0
        self.done: list[str] = []
        self._render()

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

    STEPS = ("Privileged roles", "League", "Rumor channel", "Dad jokes", "Setup complete")

    def _render(self) -> None:
        self.clear_items()
        [
            self._step_roles,
            self._step_league,
            self._step_rumors,
            self._step_jokes,
            self._step_done,
        ][self.step]()

    def _embed(self) -> discord.Embed:
        last = len(self.STEPS) - 1
        embed = discord.Embed(
            title=f"WojBot setup — {self.STEPS[self.step]}",
            description=self._body(),
            colour=discord.Colour.blurple(),
        )
        if self.step < last:
            embed.set_footer(text=f"Step {self.step + 1} of {last}")
        return embed

    def _body(self) -> str:
        if self.step == 0:
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
        if self.step == 1:
            bound = configured_league(self.bot, self.guild.id)
            current = f"Currently bound to `{bound}`." if bound else "Not bound to a league yet."
            leagues = configured_leagues()
            if not leagues:
                return f"{current}\n\nNo leagues are configured in `sys_config.yml`."
            return f"{current}\n\nPick the Elo league this server drives."
        if self.step == 2:
            current = describe_channel(self.bot.configs, self.guild, RUMOR_CHANNEL_KEY)
            return (
                f"Rumors post to {current}.\n\nPick the channel `/rumor report` "
                "announces to. Without one, rumors are still recorded — they just "
                "go out to nobody."
            )
        if self.step == 3:
            current = describe_setting(self.bot.configs, self.guild.id, DAD_JOKE_KEY)
            return (
                f"Dad jokes are **{current}**.\n\nOn or off makes it this server's "
                "own, so a later change to the bot-wide default leaves it alone. "
                "*Follow default* hands the choice back."
            )
        body = "\n".join(f"• {line}" for line in self.done) or "• (nothing changed)"
        return f"**Done**\n{body}\n\n{self.cog.outstanding(self.guild)}"

    async def _advance(self, interaction: discord.Interaction) -> None:
        self.step += 1
        self._render()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    # --- step 1: roles -----------------------------------------------------

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

    # --- step 2: league ----------------------------------------------------

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
                    self.done.append(f"League bound to `{system.league}`")
                self._render()
                await interaction.edit_original_response(embed=self._embed(), view=self)

            select.callback = callback
            self.add_item(select)
        self.add_item(_NextButton(self, "Next ›"))

    # --- step 3: rumor channel ---------------------------------------------

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

    # --- step 4: dad jokes -------------------------------------------------

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

    def outstanding(self, guild: discord.Guild) -> str:
        """What still needs doing for this server, with the command for each.

        Config-only checks: everything here is a dict or file read, so the
        overview stays instant and works with the database down.
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
        channel_id = config.get(RUMOR_CHANNEL_KEY)
        if channel_id is None:
            steps.append("No rumor channel set — `/setup rumorchannel`")
        elif guild.get_channel(channel_id) is None:
            steps.append(
                f"The rumor channel (`{channel_id}`) no longer exists — "
                "`/setup rumorchannel`"
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
    @is_privileged()
    async def show(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        config = self.bot.configs.get_guild(guild.id)
        lines = []
        for key, label in TIERS:
            have, missing = _role_status(guild, config[key])
            shown = ", ".join(f"`{n}`" for n in have) or "(none)"
            gone = f" · not in this server: {', '.join(f'`{n}`' for n in missing)}" if missing else ""
            lines.append(f"**{label} roles:** {shown}{gone}")
        league = configured_league(self.bot, guild.id)
        lines.append(f"**League:** `{league}`" if league else "**League:** not bound")
        lines.append(
            f"**Rumor channel:** {describe_channel(self.bot.configs, guild, RUMOR_CHANNEL_KEY)}"
        )
        lines.append(
            f"**Dad jokes:** {describe_setting(self.bot.configs, guild.id, DAD_JOKE_KEY)}"
        )

        embed = discord.Embed(
            title=f"WojBot setup — {guild.name}",
            description="\n".join(lines) + "\n\n" + self.outstanding(guild),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(
            text="Roles: /verify · dad jokes: /dadjokes · league: /commish · "
                 "rumors: /setup rumorchannel · or walk through it with /setup wizard"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(
        name="rumorchannel", description="Set the channel reported rumors post to."
    )
    @app_commands.describe(channel="Where /rumor report announces to")
    @is_privileged()
    async def rumorchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
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
        await interaction.response.send_message(
            f"Reported rumors will post to {channel.mention}.{warning}", ephemeral=True
        )

    @group.command(name="dadjokes", description="Turn dad jokes on or off for this server.")
    @app_commands.describe(state="On, off, or follow whatever the bot-wide default is")
    @app_commands.choices(state=STATE_CHOICES)
    @is_privileged()
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
    @is_privileged()
    async def wizard(self, interaction: discord.Interaction) -> None:
        view = SetupWizard(self, interaction)
        await interaction.response.send_message(
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
