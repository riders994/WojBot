"""Annuhlitucks cog: the league's Elo ratings — display, plus the week runner.

It reads through the shared :class:`elo_system.EloSystem` for the guild's league
(see :mod:`wojbot.core.elo`), so it reuses whatever the commish cog loaded. The
ratings frames are member-indexed with ``week_N`` columns; a member's current
rating is their latest populated week.

``/elo standings`` and ``/elo team`` are public reads over those frames.
``/elo run`` is the v1 ``run_elos`` command brought forward: it scrapes the
season and rates one week, a ``3:8`` range of weeks, or — with no week given —
the latest week the platform has scored. Because it scrapes and rewrites
ratings it is commissioner-gated, unlike the rest of the group. It overlaps
``/commish publish``, which runs a whole season in one go; this is the
week-scoped version for keeping the current season up to date.

The pipeline it drives lives in :mod:`wojbot.core.elo_run`, not here, because
this cog is no longer the only thing that runs it: the **weekly Elo process**
runs the same pipeline unattended (see :mod:`wojbot.core.weekly`). This cog is
where that process is registered, since this is the cog that owns Elo.

``/elo maxpf`` is the other half of the weekly max-PF report (see
:mod:`wojbot.core.maxpf`): the process posts the week just gone every Monday,
and this is how anybody Verified asks for the season to date — or for a week
they missed — without waiting for a Monday. It is a **read**, which is why it is
not commissioner-gated like ``/elo run``: it computes nothing the league keeps
and writes nothing down.

It is also the one command here that can take minutes, so it acknowledges
privately and posts the finished report into the channel, rather than holding an
interaction open longer than Discord allows one to be held.

**Once the weekly process is on for a server, ``/elo run`` stops calculating.**
It reports what the weekly run last produced and points at ``/weekly run elo``
for a run right now. That is the whole intent of putting Elo on a schedule: the
ratings a league reads should be the ones the weekly run published, not whatever
the last person to type a command happened to compute — and a league whose
ratings depend on who ran what, when, is a league arguing about ratings.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..core.checks import is_admin, is_verified
from ..core.elo import ELO_REPORT_KEY, get_or_build, ratings_config
from ..core.maxpf import (
    MaxPfError,
    guild_today,
    league_id_for_guild,
    query_report,
    render_report as render_maxpf_report,
)
from ..core.elo_run import (
    MAX_ROWS,
    MEDALS,
    elo_process,
    in_league,
    member_names,
    render_report,
    run_weeks,
)
from ..core.weekly import PROCESS_ELO, describe_schedule, is_enabled, process_task

log = logging.getLogger(__name__)
def _latest_series(frame):
    """Latest populated rating per member, sorted high to low (or None)."""
    if frame is None or getattr(frame, "empty", True):
        return None
    return frame.ffill(axis=1).iloc[:, -1].sort_values(ascending=False)


def _season_year(system, seasons, season: int | None):
    """``season``, else the current sports year, else the latest on file."""
    if season is not None:
        return season
    current = (system.ratings_config or {}).get("current_sports_year")
    return current if current in seasons else (max(seasons) if seasons else None)
class Annuhlitucks(commands.Cog):
    """League Elo ratings and standings."""

    group = app_commands.Group(
        name="elo",
        description="League Elo ratings.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Registered here rather than in cogs/weekly.py: this is the cog that
        # owns Elo, and the two load in an arbitrary order (see
        # wojbot.core.weekly.ProcessRegistry). Registering only offers it --
        # nothing runs until a server adds and enables it.
        bot.weekly.register(elo_process())
        # Guilds with a max-PF report in flight. A set rather than a lock: the
        # second caller should be told it is already happening, not queued
        # behind a run that takes minutes.
        self._building: set[int] = set()

    async def _system(self, interaction: discord.Interaction):
        """Resolve (building if needed) this guild's EloSystem, or report why not."""
        try:
            return await get_or_build(self.bot, interaction.guild_id)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the user
            log.exception("Failed to resolve Elo system")
            await interaction.followup.send(
                f"Couldn't load this server's league: `{exc}`\n"
                "A commissioner may need to run `/commish load` first."
            )
            return None

    @group.command(name="standings", description="Show current Elo standings.")
    @app_commands.describe(
        season="Season year (defaults to the current one)",
        dynasty="Show the dynasty ratings (of that season's teams) instead of one season's",
    )
    async def standings(
        self,
        interaction: discord.Interaction,
        season: int | None = None,
        dynasty: bool = False,
    ) -> None:
        await interaction.response.defer()
        system = await self._system(interaction)
        if system is None:
            return

        def _work():
            league = system.elo_league
            # Loading reads the config — no scrape — and is what puts the
            # seasons (and so the team names) on the league.
            if not league.loaded:
                league.load()
            if league.frame_manager is None:
                system.load_frames(None)
            fm = league.frame_manager
            names = member_names(league)
            if dynasty:
                year = _season_year(system, league.seasons, season)
                return "dynasty", in_league(_latest_series(fm.dynasty_elo), league, year), names
            seasons = fm.seasonal_elo
            year = _season_year(system, seasons, season)
            if year not in seasons:
                raise ValueError(
                    f"No ratings for season {season}. Available: "
                    + (", ".join(str(y) for y in sorted(seasons)) or "none")
                )
            return str(year), _latest_series(seasons[year]), names

        try:
            label, series, names = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the user
            await interaction.followup.send(f"Couldn't build standings: `{exc}`")
            return
        if series is None or series.empty:
            await interaction.followup.send(f"No ratings recorded for {label} yet.")
            return

        name = (system.ratings_config or {}).get("league_name", system.league)
        lines = [
            f"{MEDALS.get(rank, f'`{rank:>2}`')} **{names.get(member, member)}** — {rating:.0f}"
            for rank, (member, rating) in enumerate(series.items(), start=1)
        ]
        embed = discord.Embed(
            title=f"{name} — {label} standings",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await interaction.followup.send(embed=embed)

    @group.command(name="team", description="Show one member's current Elo.")
    @app_commands.describe(
        member="Team name (or the platform account id)",
        season="Season year (defaults to the current one)",
        dynasty="Look up the dynasty rating (among that season's teams) instead",
    )
    async def team(
        self,
        interaction: discord.Interaction,
        member: str,
        season: int | None = None,
        dynasty: bool = False,
    ) -> None:
        await interaction.response.defer()
        system = await self._system(interaction)
        if system is None:
            return

        def _work():
            league = system.elo_league
            # Loading reads the config — no scrape — and is what puts the
            # seasons (and so the team names) on the league.
            if not league.loaded:
                league.load()
            if league.frame_manager is None:
                system.load_frames(None)
            fm = league.frame_manager
            names = member_names(league)
            if dynasty:
                frame, label = fm.dynasty_elo, "dynasty"
                year = _season_year(system, league.seasons, season)
            else:
                seasons = fm.seasonal_elo
                year = _season_year(system, seasons, season)
                if year not in seasons:
                    raise ValueError(
                        f"No ratings for season {season}. Available: "
                        + (", ".join(str(y) for y in sorted(seasons)) or "none")
                    )
                frame, label = seasons[year], str(year)
            series = _latest_series(frame)
            if series is None:
                return label, None
            everyone = series
            if dynasty:
                # Ranked among the league as it stands, like /elo standings.
                series = in_league(series, league, year)
            # Match on the team name case-insensitively: exact first, else a
            # unique substring. The frame is indexed by platform account id —
            # a readable owner name on Fantrax, a bare number on Sleeper — so
            # the search runs over the names, with the id kept as a fallback
            # for a member no season has a name for.
            labels = {i: str(names.get(i, i)) for i in series.index}
            query = member.strip().lower()
            match = next((i for i, n in labels.items() if n.lower() == query), None)
            if match is None:
                match = next((i for i in labels if str(i).lower() == query), None)
            if match is None:
                hits = [i for i, n in labels.items() if query in n.lower()]
                if len(hits) == 1:
                    match = hits[0]
                elif len(hits) > 1:
                    raise ValueError(
                        "Matches several members: " + ", ".join(labels[h] for h in hits)
                    )
            if match is None:
                gone = [
                    str(names.get(i, i)) for i in everyone.index
                    if i not in series.index
                    and (query in str(names.get(i, i)).lower() or str(i).lower() == query)
                ]
                if gone:
                    raise ValueError(
                        f"{', '.join(gone)} isn't in the league in {year}, so has no "
                        "dynasty standing to show."
                    )
                raise ValueError(f"No member matching '{member}'.")
            rating = float(series.loc[match])
            rank = list(series.index).index(match) + 1
            # Change since the previous recorded week.
            row = frame.loc[match].dropna()
            delta = float(row.iloc[-1] - row.iloc[-2]) if len(row) >= 2 else 0.0
            return label, (labels[match], rating, rank, len(series), delta)

        try:
            label, result = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the user
            await interaction.followup.send(f"Couldn't look that up: `{exc}`")
            return
        if result is None:
            await interaction.followup.send(f"No ratings recorded for {label} yet.")
            return

        matched, rating, rank, total, delta = result
        trend = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
        name = (system.ratings_config or {}).get("league_name", system.league)
        embed = discord.Embed(
            title=f"{matched} — {label}",
            description=(
                f"**Elo:** {rating:.0f}  ({trend} {abs(delta):.0f} last week)\n"
                f"**Rank:** {rank} of {total}"
            ),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=f"{name}")
        await interaction.followup.send(embed=embed)

    @group.command(
        name="run",
        description="Run the ratings for a week, a range of weeks, or the current week.",
    )
    @app_commands.describe(
        week="Week number (5), a range (3:8), or blank for the latest scored week",
        season="Season year (defaults to the current one)",
        overwrite="Recalculate weeks that are already rated",
        publish="Save the results to the configured backend",
    )
    @is_admin()
    async def run(
        self,
        interaction: discord.Interaction,
        week: str | None = None,
        season: int | None = None,
        overwrite: bool = False,
        publish: bool = True,
    ) -> None:
        await interaction.response.defer()
        # The hand-off. With the weekly process on, this server's ratings are
        # something the schedule produces, and a command that quietly rated a
        # different set of weeks off a fresh scrape would be a second answer to
        # a question that is supposed to have one.
        if await is_enabled(self.bot, interaction.guild_id, PROCESS_ELO):
            await interaction.followup.send(embed=await self._weekly_embed(interaction))
            return

        system = await self._system(interaction)
        if system is None:
            return
        setting = self.bot.configs.get_guild(interaction.guild_id).get(ELO_REPORT_KEY)
        try:
            result = await asyncio.to_thread(
                run_weeks,
                system,
                spec=week,
                season=season,
                overwrite=overwrite,
                publish=publish,
                report=setting,
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason to the caller
            log.exception("elo run failed")
            await interaction.followup.send(f"Couldn't run those weeks: `{exc}`")
            return
        if publish:
            self.bot.leagues.invalidate(interaction.guild_id)

        embeds = [
            discord.Embed(
                title=f"{result.league_name} — {result.year} {result.span}{result.label(report)}",
                description=render_report(report, result.names, max_rows=MAX_ROWS),
                colour=discord.Colour.blurple(),
            )
            for report in result.reports
        ]
        embeds[-1].set_footer(
            text=(
                f"Published via {system.writer_key}."
                if publish
                else "Not published — these ratings are in memory only."
            )
        )
        await interaction.followup.send(embeds=embeds)

    @group.command(
        name="maxpf",
        description="Max points for: the season to date, or one week.",
    )
    @app_commands.describe(
        week="One matchup period (default: the whole season to date)"
    )
    @is_verified()
    async def maxpf(
        self, interaction: discord.Interaction, week: int | None = None
    ) -> None:
        guild_id = interaction.guild_id
        try:
            league_id = league_id_for_guild(self.bot, guild_id)
        except MaxPfError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        # One at a time per server. Two of these at once means two scrapes of
        # the same league and two runs of the same optimisation, for one report.
        if guild_id in self._building:
            await interaction.response.send_message(
                "I'm already building a max-PF report for this server — it'll "
                "post in the channel it was asked for.",
                ephemeral=True,
            )
            return

        scope = f"week {week}" if week is not None else "the season to date"
        # Answered rather than deferred: a season report outlives the fifteen
        # minutes a deferred interaction gets, and the report itself goes to the
        # channel, so nothing important depends on this token still being good.
        await interaction.response.send_message(
            f"Working out max PF for {scope} — this takes a few minutes (longer "
            "the first time, while the box scores are fetched). I'll post it here.",
            ephemeral=True,
        )

        self._building.add(guild_id)
        try:
            report = await asyncio.to_thread(
                query_report,
                league_id,
                guild_today(self.bot, guild_id),
                self._league_name(guild_id),
                week=week,
            )
        except MaxPfError as exc:
            await self._quietly(interaction, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface the reason to the caller
            log.exception("max-pf report failed")
            await self._quietly(interaction, f"Couldn't build that report: `{exc}`")
            return
        finally:
            self._building.discard(guild_id)

        if report is None:
            await self._quietly(
                interaction,
                "No matchup period has finished yet, so there's nothing to report.",
            )
            return

        embed = discord.Embed(
            title=report.title,
            description=render_maxpf_report(report),
            colour=discord.Colour.dark_teal(),
        )
        embed.set_footer(text=f"asked for by {interaction.user.display_name}")
        channel = interaction.channel
        if channel is None:  # no channel to post into; hand it back privately
            await self._quietly(interaction, render_maxpf_report(report))
            return
        try:
            await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            log.warning("Couldn't post a max-PF report", exc_info=exc)
            await self._quietly(interaction, f"The report wouldn't post: `{exc}`")
            return
        await self._quietly(interaction, f"Posted the max-PF report for {scope}.")

    def _league_name(self, guild_id: int | None) -> str:
        """What the league calls itself, for the report's title."""
        return (
            (ratings_config(self.bot, guild_id) or {}).get("league_name")
            or "The league"
        )

    @staticmethod
    async def _quietly(interaction: discord.Interaction, message: str) -> None:
        """Follow up, accepting that the interaction may have expired.

        A max-PF run can outlast the fifteen minutes Discord keeps a token
        alive. The report is already in the channel by then, so a dead token is
        a lost note rather than a lost report.
        """
        try:
            await interaction.followup.send(message, ephemeral=True)
        except discord.HTTPException:
            log.debug("The max-PF interaction had expired by the time it finished")

    async def _weekly_embed(self, interaction: discord.Interaction) -> discord.Embed:
        """What to say instead of rating, when the weekly process owns the job."""
        embed = discord.Embed(
            title="Elo runs on a schedule here",
            description=(
                "This server's ratings are produced by the weekly Elo process, "
                "so `/elo run` doesn't calculate them any more. "
                "`/weekly run process:elo` runs it right now, and "
                "`/weekly disable process:elo` hands the job back to this command."
            ),
            colour=discord.Colour.blurple(),
        )
        try:
            task = await process_task(self.bot, interaction.guild_id, PROCESS_ELO)
        except Exception:  # noqa: BLE001 - the sentence above is the useful part
            log.debug("Couldn't read the weekly Elo process", exc_info=True)
            return embed
        if task is None:  # switched off between the check and here; say nothing wrong
            return embed
        embed.add_field(name="When", value=describe_schedule(task), inline=False)
        if task.next_fire_at is not None:
            embed.add_field(
                name="Next",
                value=f"<t:{int(task.next_fire_at.timestamp())}:F>",
                inline=False,
            )
        if task.last_fired_at is not None:
            embed.add_field(
                name="Last run",
                value=f"<t:{int(task.last_fired_at.timestamp())}:R>",
                inline=False,
            )
        if task.last_error:
            embed.add_field(name="⚠️ Last run", value=task.last_error, inline=False)
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Annuhlitucks(bot))
