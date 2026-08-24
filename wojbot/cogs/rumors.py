"""Report and read league rumors.

``/rumor report`` walks a manager through a rumor -- who is saying it (the
source type), how loudly it is being said (the release type), the sentence it
goes in (the form), and the text itself -- then records it and posts it to the
channel
``/setup rumorchannel`` names. ``/rumor recent`` reads them back.

The leaker comes first because that is the choice a reporter has already made
before they start typing: they know who is talking, and how far the story
travels follows from how senior that person is.

Unlike every other grouped command in the bot, ``/rumor`` is deliberately **not**
``guild_only``. Half the point of a rumor is that nobody watched you file it, so
a manager can run the whole wizard in a DM; the league is worked out from who
they are rather than from where they typed, and a manager in more than one *this
season* gets asked which. Leagues they have left are not offered — there is
nothing to report in one, and the choice is noise.

The rules about which sources may carry which release types, and the format
strings both are written in, live in :mod:`wojbot.core.rumor`.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..core import rumor as rumors
from ..core.checks import ADMIN_ROLES_KEY, has_privilege_in_guild
from ..core.format import chunk
from ..core.rumor import RUMOR_CHANNEL_KEY

log = logging.getLogger(__name__)

WIZARD_TIMEOUT = 300.0
DEFAULT_RECENT = 5
# The cap itself lives in core.rumor, which enforces it on the way to the
# database. Setting it on the TextInput too just means the reporter finds out
# while they are still typing rather than at the review step.
MAX_RUMOR_LENGTH = rumors.MAX_RUMOR_LENGTH

# Release types climb from level 0 (idle talk) to 5 (the league speaking), and
# the colour climbs with them so the weight of one reads before the words do.
RELEASE_COLOURS = {
    0: discord.Colour.light_grey(),
    1: discord.Colour.blue(),
    2: discord.Colour.teal(),
    3: discord.Colour.green(),
    4: discord.Colour.orange(),
    5: discord.Colour.gold(),
}


def rumor_embed(release_name: str, release_level: int, source_text: str, body: str):
    """The public presentation of one rumor.

    Deliberately says nothing about who reported it: the source type is the
    attribution, and naming the manager behind it would defeat the mechanic.
    """
    embed = discord.Embed(
        title=f"🗞️ {release_name.title()}",
        description=body,
        colour=RELEASE_COLOURS.get(release_level, discord.Colour.blurple()),
    )
    embed.set_footer(text=f"— {source_text}")
    return embed


def _truncate(text: str, limit: int) -> str:
    """Fit text into a Discord component label/description."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


class RumorError(Exception):
    """Something the reporter needs to fix before they can report anything."""


class RumorTextModal(discord.ui.Modal):
    """Where the reporter actually types the rumor.

    A modal rather than an inline component because it is the only way Discord
    offers to take free text, and it can only be opened in response to a fresh
    interaction -- which is why the step that leads here is a button.
    """

    def __init__(self, wizard: "RumorWizard", field: str) -> None:
        super().__init__(title="Report a rumor", timeout=WIZARD_TIMEOUT)
        self.wizard = wizard
        self.field = field
        self.entry = discord.ui.TextInput(
            label=field.replace("_", " ").title()[:45],
            style=discord.TextStyle.paragraph,
            max_length=MAX_RUMOR_LENGTH,
            required=True,
            default=wizard.fills.get(field),
            # The form's own text, so it's clear what the words land inside of.
            placeholder=_truncate(wizard.form.text, 100),
        )
        self.add_item(self.entry)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.entry.value.strip()
        # Kept either way, so 'Rewrite it' reopens with what they typed rather
        # than making them start the paragraph again.
        self.wizard.fills[self.field] = text
        try:
            rumors.check_fill_length(text)
        except rumors.RumorTooLong as exc:
            # Only reachable if max_length didn't hold; say so here rather than
            # letting it fail at the write, two steps later.
            self.wizard.note = f"⚠️ {exc}"
        else:
            self.wizard.note = ""
            self.wizard.step += 1
        self.wizard._render()
        await interaction.response.edit_message(
            embed=self.wizard._embed(), view=self.wizard
        )


class RumorWizard(discord.ui.View):
    """One ephemeral message, rebuilt in place for each choice.

    Built through :meth:`create` rather than the constructor, because working
    out who the reporter is takes several queries and a view cannot be built
    asynchronously.
    """

    def __init__(self, cog: "Rumors", interaction: discord.Interaction) -> None:
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.cog = cog
        self.bot = cog.bot
        self.user = interaction.user
        self.invoking_guild_id = interaction.guild_id

        self.dims: rumors.RumorDims | None = None
        self.manager_id: int | None = None
        self.leagues: list[rumors.League] = []
        self.league: rumors.League | None = None
        self.reporter: rumors.Reporter | None = None
        self.privileged = False

        self.release: rumors.ReleaseType | None = None
        self.source: rumors.SourceType | None = None
        self.form: rumors.RumorForm | None = None
        self.fills: dict[str, str] = {}

        self.step = 0
        self.note = ""
        # Set by a step that found nothing to offer, so the embed shows the
        # explanation instead of prompting for a choice that isn't there.
        self.stuck = False

    @classmethod
    async def create(cls, cog: "Rumors", interaction: discord.Interaction) -> "RumorWizard":
        """Resolve the reporter and their league(s), then build the first step.

        Raises:
            RumorError: with a message meant for the reporter, when there is
                nothing to report as.
        """
        view = cls(cog, interaction)
        view.dims = await cog.dims()
        view.manager_id = await rumors.resolve_manager(view.bot, interaction.user)
        if view.manager_id is None:
            raise RumorError(
                "You aren't linked to a manager yet, so I don't know whose front "
                "office you'd be speaking for. A commissioner can do that with "
                "`/commish db linkuser`."
            )

        if interaction.guild_id is not None:
            league = await rumors.league_for_guild(view.bot, interaction.guild_id)
            if league is None:
                raise RumorError(
                    "This server isn't linked to a league — `/commish db link`."
                )
            view.leagues = [league]
        else:
            view.leagues = await rumors.leagues_for_manager(view.bot, view.manager_id)
            if not view.leagues:
                raise RumorError(
                    "You don't have a team in any league this season, so there's "
                    "no front office to speak for."
                )

        if len(view.leagues) == 1:
            await view.set_league(view.leagues[0])
        view._render()
        return view

    async def set_league(self, league: rumors.League) -> None:
        """Bind the wizard to a league, resolving who the reporter is in it."""
        self.league = league
        self.reporter = await rumors.resolve_reporter(self.bot, self.manager_id, league)
        # The Commissioner is a source you have to be trusted with, and trust is
        # per-server. In a DM there is no guild on the interaction, so it is the
        # league's own server that decides.
        self.privileged = await has_privilege_in_guild(
            self.bot,
            league.guild_id or self.invoking_guild_id,
            self.user,
            ADMIN_ROLES_KEY,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This wizard belongs to whoever started it.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        self.clear_items()

    # --- steps -------------------------------------------------------------

    def _steps(self) -> list[tuple[str, object]]:
        """The steps for this run. The league step only exists when it's a real choice."""
        steps: list[tuple[str, object]] = []
        if len(self.leagues) > 1:
            steps.append(("League", self._step_league))
        steps += [
            ("Source", self._step_source),
            ("Release type", self._step_release),
            ("Form", self._step_form),
        ]
        # A form with nothing to fill has nothing to type; dropping the step
        # from the list is what skips it, so that a step builder never has to
        # re-enter _render to move past itself.
        if self.form is None or self.form.required_fills:
            steps.append(("The rumor", self._step_text))
        steps.append(("Review", self._step_review))
        return steps

    def _render(self) -> None:
        self.clear_items()
        self.stuck = False
        steps = self._steps()
        self.step = max(0, min(self.step, len(steps) - 1))
        steps[self.step][1]()
        if self.step:
            self.add_item(_BackButton(self))

    def _embed(self) -> discord.Embed:
        steps = self._steps()
        colour = (
            RELEASE_COLOURS.get(self.release.level, discord.Colour.blurple())
            if self.release else discord.Colour.blurple()
        )
        embed = discord.Embed(
            title=f"Report a rumor — {steps[self.step][0]}",
            description=self._body(),
            colour=colour,
        )
        embed.set_footer(text=f"Step {self.step + 1} of {len(steps)}")
        return embed

    def _chosen(self) -> str:
        """A running summary of what's been picked, so each step has context."""
        lines = []
        if self.league and len(self.leagues) > 1:
            lines.append(f"**League:** {self.league.name}")
        if self.source and self.reporter:
            lines.append(f"**Source:** {self._source_text(self.source)}")
        if self.release:
            lines.append(f"**Release:** {self.release.name}")
        if self.form:
            lines.append(f"**Form:** {self.form.title}")
        return "\n".join(lines)

    def _source_text(self, source: rumors.SourceType) -> str:
        return rumors.render_source(
            source,
            team_name=self.reporter.team_name,
            manager_name=self.reporter.display_name,
        )

    def _preview(self) -> str:
        """The rumor as it will read. Same two passes the read path makes, so
        what the reporter approves is what everyone else later sees."""
        return rumors.fill_fields(
            rumors.render_form(self.form, self.fills),
            team_name=self.reporter.team_name,
            manager_name=self.reporter.display_name,
        )

    def _body(self) -> str:
        chosen = self._chosen()
        prefix = f"{chosen}\n\n" if chosen else ""
        note = f"{self.note}\n\n" if self.note else ""
        name = self._steps()[self.step][0]

        if self.stuck:
            # Nothing to choose from; the note is the whole message.
            return f"{note}{chosen}".rstrip()
        if name == "League":
            body = "Which league is this rumor about?"
        elif name == "Source":
            body = (
                "Who's talking? The more senior the leaker, the further this "
                "can be allowed to travel."
            )
        elif name == "Release type":
            body = (
                f"How is this getting out? *{self._source_text(self.source)}* "
                "can carry any of these."
            )
        elif name == "Form":
            body = "How should it read?"
        elif name == "The rumor":
            body = (
                f"> {self._preview()}\n\nWrite the rumor."
                if self.fills else "Write the rumor."
            )
        else:
            body = (
                f"**{self.release.name.title()}**\n> {self._preview()}\n"
                f"— *{self._source_text(self.source)}*\n\n"
                "Post it?"
            )
        return f"{note}{prefix}{body}"

    async def _advance(self, interaction: discord.Interaction) -> None:
        self.step += 1
        self._render()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    # --- step: league ------------------------------------------------------

    def _step_league(self) -> None:
        select = discord.ui.Select(
            placeholder="Which league?",
            options=[
                discord.SelectOption(
                    label=_truncate(league.name, 100),
                    value=str(league.id),
                    default=(self.league is not None and league.id == self.league.id),
                )
                for league in self.leagues[:25]
            ],
        )

        async def callback(interaction: discord.Interaction, select=select):
            league = next(
                lg for lg in self.leagues if lg.id == int(select.values[0])
            )
            await interaction.response.defer()
            await self.set_league(league)
            # Team and display name change with the league, and so does who may
            # speak as the Commissioner -- everything downstream is stale.
            self.release = self.source = self.form = None
            self.note = "" if self.reporter else (
                f"⚠️ You have no team in **{league.name}**, so there's no front "
                "office to speak for. Pick another league."
            )
            if self.reporter:
                self.step += 1
            self._render()
            await interaction.edit_original_response(embed=self._embed(), view=self)

        select.callback = callback
        self.add_item(select)

    # --- step: source ------------------------------------------------------

    def _step_source(self) -> None:
        options = rumors.reportable_sources(
            self.dims.releases,
            self.dims.sources,
            self.dims.forms,
            privileged=self.privileged,
        )
        if not options:
            # Every reporter has at least the quiet end of the list, so this
            # only fires if the dims lose their enabled forms entirely.
            self.note = "There's nobody you can report as right now."
            self.stuck = True
            return
        select = discord.ui.Select(
            placeholder="Who's talking?",
            options=[
                discord.SelectOption(
                    label=_truncate(self._source_text(source), 100),
                    value=str(source.id),
                    description=f"Level {source.level}"
                                + ("" if source.is_internal else " · outside the club"),
                    default=(self.source is not None and source.id == self.source.id),
                )
                for source in options[:25]
            ],
        )

        async def callback(interaction: discord.Interaction, select=select):
            self.source = self.dims.source(int(select.values[0]))
            # How far this one can travel is the source's to decide, so a
            # release picked under the old leaker no longer means anything.
            self.release = self.form = None
            self.note = ""
            await self._advance(interaction)

        select.callback = callback
        self.add_item(select)

    # --- step: release type ------------------------------------------------

    def _step_release(self) -> None:
        options = rumors.releases_for_source(
            self.source, self.dims.releases, self.dims.forms
        )
        if not options:
            # reportable_sources already drops sources with nothing to carry,
            # so this only fires if the two ever disagree.
            self.note = (
                f"*{self._source_text(self.source)}* has no way to get this "
                "out. Go back and pick another source."
            )
            self.stuck = True
            return
        select = discord.ui.Select(
            placeholder="How is it getting out?",
            options=[
                discord.SelectOption(
                    label=_truncate(release.name.title(), 100),
                    value=str(release.id),
                    description=f"Authority level {release.level}",
                    default=(self.release is not None and release.id == self.release.id),
                )
                for release in options[:25]
            ],
        )

        async def callback(interaction: discord.Interaction, select=select):
            self.release = self.dims.release(int(select.values[0]))
            # A form that fit the old release may not fit this one.
            self.form = None
            self.note = ""
            await self._advance(interaction)

        select.callback = callback
        self.add_item(select)

    # --- step: form --------------------------------------------------------

    def _step_form(self) -> None:
        options = rumors.forms_for(self.release, self.source, self.dims.forms)
        if not options:
            self.note = (
                "No form fits that release and source together. Go back and "
                "change one of them."
            )
            self.stuck = True
            return
        select = discord.ui.Select(
            placeholder="How should it read?",
            options=[
                discord.SelectOption(
                    label=_truncate(form.title, 100),
                    value=str(form.id),
                    description=_truncate(form.text, 100),
                    default=(self.form is not None and form.id == self.form.id),
                )
                for form in options[:25]
            ],
        )

        async def callback(interaction: discord.Interaction, select=select):
            self.form = self.dims.form(int(select.values[0]))
            # Fills belong to a form; keep only the ones the new one asks for.
            self.fills = {
                k: v for k, v in self.fills.items() if k in self.form.required_fills
            }
            self.note = ""
            await self._advance(interaction)

        select.callback = callback
        self.add_item(select)

    # --- step: text --------------------------------------------------------

    def _step_text(self) -> None:
        # _steps only includes this step for a form that asks for something.
        field = self.form.required_fills[0]
        button = discord.ui.Button(
            label="Rewrite it" if self.fills else "Write it",
            style=discord.ButtonStyle.primary,
        )

        async def callback(interaction: discord.Interaction, field=field):
            await interaction.response.send_modal(RumorTextModal(self, field))

        button.callback = callback
        self.add_item(button)

    # --- step: review ------------------------------------------------------

    def _step_review(self) -> None:
        post = discord.ui.Button(label="Post it", style=discord.ButtonStyle.success)
        post.callback = self._submit
        self.add_item(post)

        cancel = discord.ui.Button(label="Discard", style=discord.ButtonStyle.danger)

        async def discard(interaction: discord.Interaction):
            self.clear_items()
            self.stop()
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="Discarded",
                    description="Nothing was recorded.",
                    colour=discord.Colour.dark_grey(),
                ),
                view=self,
            )

        cancel.callback = discard
        self.add_item(cancel)

    async def _submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            rumor_id, _reported_at = await rumors.record_rumor(
                self.bot,
                self.reporter,
                release=self.release,
                source=self.source,
                form=self.form,
                fills=self.fills,
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason to the reporter
            log.exception("recording a rumor failed")
            self.note = f"⚠️ Couldn't record that: `{exc}`"
            self._render()
            await interaction.edit_original_response(embed=self._embed(), view=self)
            return

        embed = rumor_embed(
            self.release.name,
            self.release.level,
            self._source_text(self.source),
            self._preview(),
        )
        where = await self.cog.broadcast(self.league, embed)

        self.clear_items()
        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Filed",
                description=f"Rumor `#{rumor_id}` recorded. {where}",
                colour=RELEASE_COLOURS.get(self.release.level, discord.Colour.blurple()),
            ),
            view=self,
        )


class _BackButton(discord.ui.Button):
    def __init__(self, wizard: RumorWizard) -> None:
        super().__init__(label="‹ Back", style=discord.ButtonStyle.secondary, row=4)
        self._wizard = wizard

    async def callback(self, interaction: discord.Interaction) -> None:
        self._wizard.step -= 1
        self._wizard.note = ""
        self._wizard._render()
        await interaction.response.edit_message(
            embed=self._wizard._embed(), view=self._wizard
        )


class Rumors(commands.Cog):
    """Reporting and reading the league's rumor mill."""

    group = app_commands.Group(
        name="rumor",
        description="Report a rumor, or read the recent ones.",
        # Not guild_only, unlike the bot's other groups: reporting from a DM is
        # the point. See the module docstring.
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._dims: rumors.RumorDims | None = None

    async def dims(self) -> rumors.RumorDims:
        """The lookup dimensions, read once and kept for the bot's lifetime."""
        if self._dims is None:
            self._dims = await rumors.load_dims(self.bot.sql)
        return self._dims

    def _sql_ready(self) -> str | None:
        """The reason the database can't be used right now, if there is one."""
        sql = getattr(self.bot, "sql", None)
        if sql is None:
            return "No database is configured, so rumors can't be recorded."
        if sql.connection is None:
            return "The database is down. A bot owner can try `/sql reconnect`."
        return None

    async def broadcast(self, league: rumors.League, embed: discord.Embed) -> str:
        """Post a rumor to the league's rumor channel; describe what happened.

        Never raises: by the time this runs the rumor is already recorded, and
        a failure to announce it should read as a failure to announce it, not
        as having lost the rumor.
        """
        if league.guild_id is None:
            return "It isn't posted anywhere — this league isn't bound to a server."
        channel_id = self.bot.configs.get_guild(league.guild_id).get(RUMOR_CHANNEL_KEY)
        if channel_id is None:
            return "It isn't posted anywhere yet — set a channel with `/setup rumorchannel`."
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return (
                    f"The rumor channel (`{channel_id}`) is gone — "
                    "point `/setup rumorchannel` at a new one."
                )
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            return f"I can't post in {channel.mention} — it's recorded but unannounced."
        except discord.HTTPException as exc:
            log.warning("Rumor broadcast failed: %s", exc)
            return "Posting it failed, but it's recorded."
        return f"Posted to {channel.mention}."

    @group.command(name="report", description="Report a rumor to the league.")
    async def report(self, interaction: discord.Interaction) -> None:
        problem = self._sql_ready()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            view = await RumorWizard.create(self, interaction)
        except RumorError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - surface the reason to the reporter
            log.exception("rumor wizard failed to start")
            await interaction.followup.send(f"Couldn't start that: `{exc}`", ephemeral=True)
            return
        if view.reporter is None and len(view.leagues) == 1:
            await interaction.followup.send(
                f"You have no team in **{view.leagues[0].name}** this season, so "
                "there's no front office to speak for.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=view._embed(), view=view, ephemeral=True)

    @group.command(name="recent", description="Show the league's most recent rumors.")
    @app_commands.describe(
        count="How many to show (default 5; the whole season is the maximum)",
        league="Which league — only needed in a DM if you're in more than one",
    )
    async def recent(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 500] = DEFAULT_RECENT,
        league: str | None = None,
    ) -> None:
        problem = self._sql_ready()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        # A listing everyone can see, so the whole reply is public -- including
        # the failures. Deferring publicly and then answering ephemerally would
        # leave a stray "thinking…" in the channel.
        await interaction.response.defer()
        try:
            target = await self._resolve_league(interaction, league)
        except RumorError as exc:
            await interaction.followup.send(str(exc))
            return

        try:
            year = await rumors.current_season(self.bot, target)
            if year is None:
                await interaction.followup.send(f"**{target.name}** has no seasons yet.")
                return
            found = await rumors.recent_rumors(self.bot, target, year, count)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the caller
            log.exception("reading recent rumors failed")
            await interaction.followup.send(f"Couldn't read those: `{exc}`")
            return

        if not found:
            await interaction.followup.send(
                f"Nothing has been reported in **{target.name}** for {year}.",
            )
            return

        header = f"**{target.name} — {year} rumor mill** ({len(found)} most recent)"
        blocks = [
            f"`#{r.id}` **{r.release_name.title()}** · *{r.source_text}* — {r.timestamp}"
            f"\n> {r.body}"
            for r in found
        ]
        # Chunked rather than clipped: asking for the season and being handed
        # the first page of it would be the wrong answer.
        for index, message in enumerate(chunk([header, *blocks], sep="\n\n")):
            await interaction.followup.send(
                message, allowed_mentions=discord.AllowedMentions.none(), silent=index > 0,
            )

    async def _resolve_league(
        self, interaction: discord.Interaction, name: str | None
    ) -> rumors.League:
        """Which league the caller means, from the server or from who they are."""
        if interaction.guild_id is not None:
            found = await rumors.league_for_guild(self.bot, interaction.guild_id)
            if found is None:
                raise RumorError("This server isn't linked to a league — `/commish db link`.")
            return found

        manager_id = await rumors.resolve_manager(self.bot, interaction.user)
        if manager_id is None:
            raise RumorError(
                "You aren't linked to a manager, so I don't know whose rumors to "
                "show you. A commissioner can link you with `/commish db linkuser`."
            )
        # This season's leagues only, same as reporting. A league somebody has
        # left is still readable -- just from its own server, where the league
        # comes from the channel rather than from who is asking.
        leagues = await rumors.leagues_for_manager(self.bot, manager_id)
        if not leagues:
            raise RumorError(
                "You don't have a team in any league this season. Ask in the "
                "league's own server and I'll know which wire you mean."
            )
        if name:
            match = next((lg for lg in leagues if lg.name == name), None)
            if match is None:
                raise RumorError(f"You're not in a league called **{name}** this season.")
            return match
        if len(leagues) > 1:
            names = ", ".join(f"**{lg.name}**" for lg in leagues)
            raise RumorError(f"You're in more than one league — say which: {names}.")
        return leagues[0]

    @recent.autocomplete("league")
    async def _league_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """The caller's leagues this season. Silent on failure — a convenience."""
        try:
            manager_id = await rumors.resolve_manager(self.bot, interaction.user)
            if manager_id is None:
                return []
            leagues = await rumors.leagues_for_manager(self.bot, manager_id)
        except Exception:  # noqa: BLE001 - an autocomplete must never raise at the user
            log.debug("league autocomplete failed", exc_info=True)
            return []
        needle = current.lower()
        return [
            app_commands.Choice(name=lg.name, value=lg.name)
            for lg in leagues if needle in lg.name.lower()
        ][:25]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Rumors(bot))
