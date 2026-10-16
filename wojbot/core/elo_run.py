"""Rating weeks: the one pipeline that turns scored weeks into Elo ratings.

Everything that *calculates* Elo goes through :func:`run_weeks`. There used to be
one copy of this in the ``/elo run`` command; the weekly league process
(:mod:`wojbot.core.weekly`) needs the same pipeline unattended, and two copies of
"scrape, seed, rate, publish" would drift apart in exactly the way that leaves a
league's ratings depending on which command last touched them.

So the split is: this module runs weeks and says what moved, the Elo cog renders
that for whoever typed the command, and the weekly process renders it into the
league's channel. :func:`weekly_elo` is the process itself, registered by the Elo
cog because that is the cog that owns Elo.

**Catching up is the unattended mode.** A command names its weeks; a weekly job
cannot, because the answer depends on what the platform scored while nobody was
looking and on whether last week's run happened at all. With ``catch_up=True``
the pipeline works out the range itself -- from the last week rated without a gap
through the last week the platform has scored -- and reports having nothing to do
rather than failing when those are the same week.

Everything here is synchronous and blocking: it scrapes a platform and moves
pandas frames around. Call it through :func:`asyncio.to_thread`, as both callers
do.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from elo_system.tools.basics import WEEK_STR

from .dynasty import last_seasonal_week
from .elo import (
    ELO_REPORT_KEY,
    REPORT_DYNASTY,
    REPORT_SEASONAL,
    get_or_build,
    report_scopes,
)
from .weekly import PROCESS_ELO, ProcessOutcome, WeeklyProcess

log = logging.getLogger(__name__)

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# Rows in a run report before it is cut short, so a large league can't outgrow
# an embed description.
MAX_ROWS = 25

# How a report names each kind of rating, for a league that has both.
SCOPE_LABELS = {REPORT_SEASONAL: "season", REPORT_DYNASTY: "dynasty"}


def parse_weeks(spec: str | None, length: int) -> list[int]:
    """Weeks to run, from a ``5`` / ``3:8`` spec -- or the latest scored week.

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


def week_column(league, year: int, week: int) -> str:
    """The column holding ``week``; dynasty weeks are offsets into one timeline."""
    if league.is_dynasty:
        week += league.seasons[year]["dynasty_start_week"]
    return WEEK_STR.format(week)


def rated_frame(league, year: int):
    """The frame a run writes into: the dynasty timeline, or this season's."""
    fm = league.frame_manager
    return fm.dynasty_elo if league.is_dynasty else fm.seasonal_elo[year]


def last_rated_week(league, year: int, frame) -> int:
    """The last week rated without a gap. Week 0 always counts -- it is the seed."""
    week = 0
    while frame.get(week_column(league, year, week + 1)) is not None:
        week += 1
    return week


def member_names(league) -> dict:
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


def in_league(series, league, year):
    """Only the members playing in ``year``, ranked among themselves.

    A dynasty frame keeps a row for everyone who has ever played, since a
    manager who comes back picks up where they left off -- but a standings
    list that still ranks somebody who left two seasons ago reads as wrong.
    A season with no members on file (never scraped) is left unfiltered
    rather than shown empty.
    """
    members = {str(m) for m in ((league.seasons.get(year) or {}).get("league_members") or {})}
    if series is None or not members:
        return series
    return series[[str(m) in members for m in series.index]]


@dataclass
class ScopeReport:
    """One kind of rating after a run: this season's, or the dynasty's."""

    scope: str
    ratings: object  # a pandas Series, member id -> rating, highest first
    deltas: dict = field(default_factory=dict)


@dataclass
class RunResult:
    """What one call to :func:`run_weeks` did.

    ``reports`` holds one :class:`ScopeReport` per kind of rating asked for,
    in order -- just the season's for a league that isn't a dynasty.
    ``up_to_date`` is the catch-up case with nothing to catch up on: not a
    failure, and not something to announce. ``weeks`` is empty then, and the
    reports still hold where everyone stands.
    """

    year: int
    weeks: list[int]
    reports: list[ScopeReport]
    names: dict
    league_name: str
    is_dynasty: bool = False
    published: bool = False
    up_to_date: bool = False

    @property
    def ratings(self):
        """The first report's ratings -- the only one, outside a dynasty."""
        return self.reports[0].ratings if self.reports else {}

    @property
    def deltas(self) -> dict:
        return self.reports[0].deltas if self.reports else {}

    @property
    def span(self) -> str:
        """"week 7" / "weeks 5–7" / "no new weeks"."""
        if not self.weeks:
            return "no new weeks"
        if len(self.weeks) == 1:
            return f"week {self.weeks[0]}"
        return f"weeks {self.weeks[0]}–{self.weeks[-1]}"

    def label(self, report: ScopeReport) -> str:
        """`` (season)`` / `` (dynasty)`` -- only a dynasty has two to tell apart."""
        return f" ({SCOPE_LABELS[report.scope]})" if self.is_dynasty else ""


def _scope_frame(league, year: int, scope: str):
    """A scope's frame, and how to name its column for one of the season's weeks."""
    if scope == REPORT_DYNASTY:
        return rated_frame(league, year), lambda w: week_column(league, year, w)
    return league.frame_manager.seasonal_elo.get(year), WEEK_STR.format


def _scope_report(league, year: int, scope: str, weeks: list[int], last: int) -> ScopeReport:
    """Where everyone stands at week ``last``, and what ``weeks`` moved."""
    frame, column = _scope_frame(league, year, scope)
    ratings = _column(frame, column(last)) if frame is not None else {}
    if scope == REPORT_DYNASTY:
        ratings = in_league(ratings, league, year) if len(ratings) else ratings
    deltas = {}
    # What these weeks moved, against the week before them. Read after the
    # run, since a season frame may only just have caught up to it.
    if (
        weeks and weeks[0] > 0 and len(ratings)
        and frame is not None and frame.get(column(weeks[0] - 1)) is not None
    ):
        before = frame[column(weeks[0] - 1)].dropna()
        deltas = {
            member: float(ratings[member] - before[member])
            for member in ratings.index.intersection(before.index)
        }
    return ScopeReport(scope, ratings, deltas)


def run_weeks(
    system,
    *,
    spec: str | None = None,
    season: int | None = None,
    overwrite: bool = False,
    publish: bool = True,
    catch_up: bool = False,
    report: str | None = None,
) -> RunResult:
    """Scrape, rate the weeks asked for, and (by default) publish the result.

    Blocking. ``spec`` names the weeks (``"5"``, ``"3:8"``, or None for the
    latest scored one); ``catch_up`` ignores it and rates everything between the
    last rated week and the last scored one. ``report`` is the server's
    ``elo_report`` setting, which picks what a dynasty league reports.

    Raises ``ValueError`` for anything the caller can fix -- an unknown season, a
    week that isn't scored yet, a gap that has to be filled first -- so both
    callers can put the reason in front of somebody.
    """
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

    length = league.seasons[year].get("current_season_length") or 0
    # Week 0 seeds the season — and for a dynasty carries the previous season's
    # ratings over — so it has to exist before any week is rated. overwrite=False
    # leaves a seed already on file alone.
    league.frame_manager.generate(year, overwrite=False)
    frame = rated_frame(league, year)
    scopes = report_scopes(report, league.is_dynasty)

    names = member_names(league)
    league_name = (system.ratings_config or {}).get("league_name", system.league)

    # A dynasty league rates the season's own frame too, off the same scores --
    # but where the season's previous week is missing, the engine skips it
    # without a word and the dynasty week lands alone. The season can fall
    # behind that way (a league rated before elo_system 2.0.2 rated only the
    # dynasty). Rerunning weeks is safe: dynasty weeks already on file are left
    # alone unless overwrite is set.
    season_rated = None
    if league.is_dynasty:
        season_rated = last_seasonal_week(league.frame_manager.seasonal_elo.get(year))

    if catch_up:
        if length < 1:
            raise ValueError("No weeks have been scored for this season yet.")
        rated = last_rated_week(league, year, frame)
        if season_rated is not None:
            # Unattended, so it repairs a season that fell behind rather than
            # stopping to ask: start from whichever frame is further back.
            rated = min(rated, season_rated)
        weeks = list(range(rated + 1, length + 1))
        if not weeks:
            # Everything the platform has scored is already rated. The common
            # case for a weekly run during an off week, and not an error.
            return RunResult(
                year=year,
                weeks=[],
                reports=[_scope_report(league, year, s, [], rated) for s in scopes],
                names=names,
                league_name=league_name,
                is_dynasty=league.is_dynasty,
                up_to_date=True,
            )
    else:
        weeks = parse_weeks(spec, length)
        # Each week is rated off the one before it, so a gap can't be skipped
        # over. The engine reports that by quietly rating nothing, so it is
        # caught here instead, while the fix can still be named.
        if weeks[0] > 0 and frame.get(week_column(league, year, weeks[0] - 1)) is None:
            rated = last_rated_week(league, year, frame)
            raise ValueError(
                f"Week {weeks[0] - 1} isn't rated yet, so week {weeks[0]} can't be. "
                f"Run `{rated + 1}:{weeks[-1]}` to catch up."
            )
        if season_rated is not None and weeks[0] > 0 and season_rated < weeks[0] - 1:
            raise ValueError(
                f"This season's own ratings stop at week {season_rated}, so week "
                f"{weeks[0]} can't be rated for it. Run `{season_rated + 1}:"
                f"{weeks[-1]}` to catch up — the dynasty weeks already rated are "
                "left as they are."
            )

    for target in weeks:
        league.run(target, year, overwrite)

    # Re-read the frames: a dynasty run rebuilds them rather than mutating.
    for scope in scopes:
        scope_frame, column = _scope_frame(league, year, scope)
        if scope_frame is None or scope_frame.get(column(weeks[-1])) is None:
            raise RuntimeError(
                f"Week {weeks[-1]} produced no ratings — the platform may not have "
                "scored it yet."
            )
    reports = [_scope_report(league, year, s, weeks, weeks[-1]) for s in scopes]
    if publish:
        system.publish()
    return RunResult(
        year=year,
        weeks=weeks,
        reports=reports,
        names=names,
        league_name=league_name,
        is_dynasty=league.is_dynasty,
        published=publish,
    )


def _column(frame, name: str):
    """One column of a frame, dropna'd and sorted high to low.

    An empty dict when the column isn't there -- a season whose seed week is
    somehow missing. It reads the same way to :func:`render_standings` as a
    frame with nobody in it, which is what the caller wants said.
    """
    column = frame.get(name)
    if column is None:
        return {}
    return column.dropna().sort_values(ascending=False)


def render_report(report: ScopeReport, names: dict, *, max_rows: int = MAX_ROWS) -> str:
    """One kind of rating as a medalled list, with what the run moved."""
    lines = []
    ratings = report.ratings
    total = len(ratings)
    for rank, (member, rating) in enumerate(ratings.items(), start=1):
        if rank > max_rows:
            lines.append(f"…and {total - max_rows} more.")
            break
        trend = ""
        if (delta := report.deltas.get(member)) is not None:
            arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
            trend = f"  ({arrow} {abs(delta):.0f})"
        label = names.get(member, member)
        lines.append(f"{MEDALS.get(rank, f'`{rank:>2}`')} **{label}** — {rating:.0f}{trend}")
    return "\n".join(lines) or "(no ratings)"


def render_standings(result: RunResult, *, max_rows: int = MAX_ROWS) -> str:
    """Every report in a run, one after another, headed when there are two."""
    if len(result.reports) == 1:
        return render_report(result.reports[0], result.names, max_rows=max_rows)
    return "\n\n".join(
        f"**{SCOPE_LABELS[r.scope].capitalize()}**\n"
        + render_report(r, result.names, max_rows=max_rows)
        for r in result.reports
    )


# --------------------------------------------------------------------------
# The weekly process
# --------------------------------------------------------------------------


async def weekly_elo(bot, task) -> ProcessOutcome:
    """Rate every week the platform has scored since the last run, and publish.

    The whole point of the weekly process: after this has run, reading the
    league's ratings is a read. It publishes through the league's configured
    writer and drops the bot's cached league data, so ``/elo standings`` the
    following morning shows the week without recomputing it.
    """
    system = await get_or_build(bot, task.guild_id)
    report = bot.configs.get_guild(task.guild_id).get(ELO_REPORT_KEY)
    result = await asyncio.to_thread(
        run_weeks, system, catch_up=True, publish=True, report=report
    )

    if result.up_to_date:
        # Nothing new to rate. Recorded on the row (fire_count moves, no error)
        # and not posted -- a weekly job that says "nothing happened" every
        # off-week is a weekly job people mute.
        return ProcessOutcome(
            title=f"{result.league_name} — nothing new",
            body=f"No new scored weeks in {result.year}; ratings are already current.",
            announce=False,
        )

    bot.leagues.invalidate(task.guild_id)
    return ProcessOutcome(
        title=f"{result.league_name} — {result.year} {result.span}",
        body=render_standings(result),
    )


def elo_process() -> WeeklyProcess:
    """The Elo weekly process, for the Elo cog to register on load."""
    return WeeklyProcess(
        name=PROCESS_ELO,
        summary="Rates every week the platform has scored, and publishes the ratings.",
        run=weekly_elo,
    )
