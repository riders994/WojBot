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
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from elo_system.tools.basics import WEEK_STR

from ..core.checks import is_commissioner
from ..core.elo import get_or_build

log = logging.getLogger(__name__)

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# Rows in a run report before it is cut short. A dynasty frame is indexed by
# every member who has ever played, which outgrows an embed description.
MAX_ROWS = 25


def _latest_series(frame):
    """Latest populated rating per member, sorted high to low (or None)."""
    if frame is None or getattr(frame, "empty", True):
        return None
    return frame.ffill(axis=1).iloc[:, -1].sort_values(ascending=False)


def _parse_weeks(spec: str | None, length: int) -> list[int]:
    """Weeks to run, from a ``5`` / ``3:8`` spec — or the latest scored week.

    ``length`` is the season's scored-period count as the platform last
    reported it, which is both the default week and the ceiling: rating a week
    beyond it would invent a result.
    """
    if spec is None or not spec.strip():
        if length < 1:
            raise ValueError("No weeks have been scored for this season yet.")
        return [length]
    parts = [p.strip() for p in spec.strip().replace("-", ":").split(":")]
    if len(parts) > 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Couldn't read '{spec}' as a week — try `5`, `3:8`, or leave it blank.")
    first, last = int(parts[0]), int(parts[-1])
    if last < first:
        raise ValueError(f"'{spec}' runs backwards — put the earlier week first.")
    if last > length:
        raise ValueError(
            f"Week {last} hasn't been scored yet — the season has {length} week(s) on file."
        )
    return list(range(first, last + 1))


def _week_column(league, year: int, week: int) -> str:
    """The column holding ``week``; dynasty weeks are offsets into one timeline."""
    if league.is_dynasty:
        week += league.seasons[year]["dynasty_start_week"]
    return WEEK_STR.format(week)


def _rated_frame(league, year: int):
    """The frame a run writes into: the dynasty timeline, or this season's."""
    fm = league.frame_manager
    return fm.dynasty_elo if league.is_dynasty else fm.seasonal_elo[year]


def _last_rated_week(league, year: int, frame) -> int:
    """The last week rated without a gap. Week 0 always counts — it is the seed."""
    week = 0
    while frame.get(_week_column(league, year, week + 1)) is not None:
        week += 1
    return week


def _member_names(league) -> dict:
    """member id -> team name. Seasons are walked in order, so the latest wins.

    A dynasty frame is indexed across every season, so the names cannot be
    taken from the one being run.
    """
    names: dict = {}
    for _, season in sorted(league.seasons.items()):
        for member_id, info in (season.get("league_members") or {}).items():
            name = info.get("curr_name") or info.get("short_name")
            if name:
                names[member_id] = name
    return names


class Annuhlitucks(commands.Cog):
    """League Elo ratings and standings."""

    group = app_commands.Group(
        name="elo",
        description="League Elo ratings.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

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
        dynasty="Show the all-time dynasty ratings instead of one season",
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
            if league.frame_manager is None:
                system.load_frames(None)
            fm = league.frame_manager
            if dynasty:
                return "dynasty", _latest_series(fm.dynasty_elo)
            seasons = fm.seasonal_elo
            year = season
            if year is None:
                current = (system.ratings_config or {}).get("current_sports_year")
                year = current if current in seasons else (max(seasons) if seasons else None)
            if year not in seasons:
                raise ValueError(
                    f"No ratings for season {season}. Available: "
                    + (", ".join(str(y) for y in sorted(seasons)) or "none")
                )
            return str(year), _latest_series(seasons[year])

        try:
            label, series = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the user
            await interaction.followup.send(f"Couldn't build standings: `{exc}`")
            return
        if series is None or series.empty:
            await interaction.followup.send(f"No ratings recorded for {label} yet.")
            return

        name = (system.ratings_config or {}).get("league_name", system.league)
        lines = [
            f"{MEDALS.get(rank, f'`{rank:>2}`')} **{member}** — {rating:.0f}"
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
        member="Team/manager name as it appears in the ratings",
        season="Season year (defaults to the current one)",
        dynasty="Look up the dynasty rating instead of one season",
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
            if league.frame_manager is None:
                system.load_frames(None)
            fm = league.frame_manager
            if dynasty:
                frame, label = fm.dynasty_elo, "dynasty"
            else:
                seasons = fm.seasonal_elo
                year = season
                if year is None:
                    current = (system.ratings_config or {}).get("current_sports_year")
                    year = current if current in seasons else (max(seasons) if seasons else None)
                if year not in seasons:
                    raise ValueError(
                        f"No ratings for season {season}. Available: "
                        + (", ".join(str(y) for y in sorted(seasons)) or "none")
                    )
                frame, label = seasons[year], str(year)
            series = _latest_series(frame)
            if series is None:
                return label, None
            # Match the member name case-insensitively: exact first, else a
            # unique substring.
            query = member.strip().lower()
            labels = [str(i) for i in series.index]
            match = next((series.index[i] for i, n in enumerate(labels) if n.lower() == query), None)
            if match is None:
                hits = [series.index[i] for i, n in enumerate(labels) if query in n.lower()]
                if len(hits) == 1:
                    match = hits[0]
                elif len(hits) > 1:
                    raise ValueError("Matches several members: " + ", ".join(str(h) for h in hits))
            if match is None:
                raise ValueError(f"No member matching '{member}'.")
            rating = float(series.loc[match])
            rank = list(series.index).index(match) + 1
            # Change since the previous recorded week.
            row = frame.loc[match].dropna()
            delta = float(row.iloc[-1] - row.iloc[-2]) if len(row) >= 2 else 0.0
            return label, (str(match), rating, rank, len(series), delta)

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
    @is_commissioner()
    async def run(
        self,
        interaction: discord.Interaction,
        week: str | None = None,
        season: int | None = None,
        overwrite: bool = False,
        publish: bool = True,
    ) -> None:
        await interaction.response.defer()
        system = await self._system(interaction)
        if system is None:
            return

        def _work():
            league = system.elo_league
            if league is None:
                raise RuntimeError("League config not loaded")
            if not league.loaded:
                league.load()
            year = season if season is not None else league.current_sports_year
            if year not in league.seasons:
                raise ValueError(
                    f"No season {year} is configured. Available: "
                    + (", ".join(str(y) for y in sorted(league.seasons)) or "none")
                )
            # Re-scrape before anything else: which week is "current" is what the
            # platform has scored now, not what an earlier command left cached.
            league.add_league(year, overwrite=True)
            # Then the ratings already on file, so rating one week continues the
            # existing frame instead of starting from a blank one.
            if league.frame_manager is None:
                system.load_frames(None)
            league.run_prep(year)

            weeks = _parse_weeks(week, league.seasons[year].get("current_season_length") or 0)
            # Week 0 seeds the season — and for a dynasty carries the previous
            # season's ratings over — so it has to exist before any week is rated.
            # overwrite=False leaves a seed already on file alone.
            league.frame_manager.generate(year, overwrite=False)

            # Each week is rated off the one before it, so a gap can't be
            # skipped over. The engine reports that by quietly rating nothing,
            # so it is caught here instead, while the fix can still be named.
            frame = _rated_frame(league, year)
            if weeks[0] > 0 and frame.get(_week_column(league, year, weeks[0] - 1)) is None:
                rated = _last_rated_week(league, year, frame)
                raise ValueError(
                    f"Week {weeks[0] - 1} isn't rated yet, so week {weeks[0]} can't be. "
                    f"Run `{rated + 1}:{weeks[-1]}` to catch up."
                )

            # Snapshot the week before the run, so the report can show what these
            # weeks moved rather than only where everyone ended up.
            before = None
            if weeks[0] > 0:
                before = frame[_week_column(league, year, weeks[0] - 1)].dropna()
            for target in weeks:
                league.run(target, year, overwrite)

            # Re-read the frame: a dynasty run rebuilds it rather than mutating.
            frame = _rated_frame(league, year)
            column = _week_column(league, year, weeks[-1])
            if frame.get(column) is None:
                raise RuntimeError(
                    f"Week {weeks[-1]} produced no ratings — the platform may not have "
                    "scored it yet."
                )
            after = frame[column].dropna().sort_values(ascending=False)
            deltas = {}
            if before is not None:
                deltas = {
                    member: float(after[member] - before[member])
                    for member in after.index.intersection(before.index)
                }
            if publish:
                system.publish()
            return year, weeks, after, deltas, _member_names(league)

        try:
            year, weeks, after, deltas, names = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the caller
            log.exception("elo run failed")
            await interaction.followup.send(f"Couldn't run those weeks: `{exc}`")
            return
        if publish:
            self.bot.leagues.invalidate(interaction.guild_id)

        lines = []
        for rank, (member, rating) in enumerate(after.items(), start=1):
            if rank > MAX_ROWS:
                lines.append(f"…and {len(after) - MAX_ROWS} more.")
                break
            trend = ""
            if (delta := deltas.get(member)) is not None:
                arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
                trend = f"  ({arrow} {abs(delta):.0f})"
            label = names.get(member, member)
            lines.append(f"{MEDALS.get(rank, f'`{rank:>2}`')} **{label}** — {rating:.0f}{trend}")

        span = f"week {weeks[0]}" if len(weeks) == 1 else f"weeks {weeks[0]}–{weeks[-1]}"
        name = (system.ratings_config or {}).get("league_name", system.league)
        embed = discord.Embed(
            title=f"{name} — {year} {span}",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(
            text=(
                f"Published via {system.writer_key}."
                if publish
                else "Not published — these ratings are in memory only."
            )
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Annuhlitucks(bot))
