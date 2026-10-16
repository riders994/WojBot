"""The max-points-for report: what the league *could* have scored.

Two ways in, one pipeline:

* :func:`weekly_report` -- the weekly process (:mod:`wojbot.core.weekly`), which
  posts the week just gone into the league's channel every Monday and goes quiet
  in the offseason.
* :func:`query_report` -- what ``/elo maxpf`` asks for: the season to date, or a
  named week, whenever somebody wants it.

They differ in exactly one rule (the recency window) and share everything else,
which is the point: a league should not be able to tell from the numbers whether
it was the schedule or a person that asked for them.

Basketball only -- the metric is nine-category head-to-head, so it means nothing
to a football league and :class:`~wojbot.core.weekly.WeeklyProcess` refuses to
let one add the process.

The metric itself is the ``max-pf`` package (github.com/riders994/basketball_max_pf,
``pip install max-pf``); none of it is reimplemented here. For each finished
matchup period it reconstructs the best lineup every manager *could* have set and
turns the gap into numbers:

* **Actual** -- the category points the team really scored.
* **M1** -- the best lineup they could have set against the opponent's actual
  lineup. Their exploitation ceiling.
* **Potential** (Actual − M1) -- points left on the table. Under the hindsight
  methodology M1 ≥ Actual always, so this is never positive: it is mismanagement
  with the projection noise stripped out.
* **Δ** (M1 − M2) -- the opponent-mismanagement dividend: points banked purely
  because the other manager didn't optimise either.

**Hindsight, no Nash.** Both readings are the quick, trustworthy ones rather
than the season study the package's own command line does:

* ``methodology="hindsight"`` -- the week is over, so realized box scores are
  available and are the trustworthy read. Projections are for weeks not yet
  played.
* whole matchup periods -- a week report covers the last one that has fully
  finished, worked out from the league's own schedule rather than assumed to be
  "last week", because a league can have an all-star break in it.
* ``nash=False`` -- the mutual-ceiling search is per-team and expensive enough to
  outlast the Discord interaction that asked for it. The read here is Actual /
  M1 / Δ / Potential; ``python -m max_pf`` is where the M3 study lives.

**Which league.** The Fantrax league id comes from the server's Elo config -- the
current season's ``league_id`` in ``resources/configs/ratings_*.yml``, the same
place ``/commish`` writes it. One place for "which league is this server", not
two, and it follows the season without anybody re-typing it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import PROJECT_ROOT
from .elo import ratings_config, season_platform
from .schedule import resolve_zone
from .tasks import TaskError
from .weekly import PROCESS_MAXPF, ProcessOutcome, WeeklyProcess

log = logging.getLogger(__name__)

# The only platform max-pf reads, and the only one this can ask about.
PLATFORM = "fantrax"

# Box scores are fetched from basketball-reference at a polite rate and cached on
# disk. Anchored to the project rather than the working directory so the cache
# survives a restart from anywhere -- a re-run that re-fetches a fortnight of box
# scores is slow and rude in equal measure.
CACHE_DIR = PROJECT_ROOT / ".cache" / "bref"

# How far back a finished period may be and still count as "this week's". Past
# this the league is between seasons or on a long break, and there is nothing to
# report -- without it the process would repost the last week of the regular
# season every Monday until the next one starts. Two weeks rather than one so a
# break, or a bot that was down for a few days, still gets its report.
REPORT_WINDOW = timedelta(days=14)

# How many teams the report lists before it stops. A 16-team league fits an embed
# comfortably; this is the guard against a league that doesn't.
MAX_ROWS = 20

# Team names are the widest column and the least informative part of a long one.
MAX_NAME = 22


class MaxPfError(Exception):
    """Something about this league's configuration stops the report happening."""


@dataclass(frozen=True)
class Report:
    """A finished max-PF report: one matchup period, or the season to date.

    ``week`` is the period a single-week report covers, and None for a season
    one. ``through`` is the last finished period either way -- for a week report
    they are the same number, and for a season report it is what "to date"
    means, which is worth saying out loud rather than leaving the reader to
    guess whether the week just gone is in it.
    """

    rows: list  # max_pf.TeamSeason, best actual first
    league_name: str
    week: int | None = None
    through: int | None = None

    @property
    def season(self) -> bool:
        return self.week is None

    @property
    def span(self) -> str:
        if not self.season:
            return f"week {self.week}"
        return f"season to date (through week {self.through})"

    @property
    def title(self) -> str:
        return f"{self.league_name} — max PF, {self.span}"

    @property
    def most_left_on_the_table(self):
        """The team that fell furthest short of its own ceiling."""
        return min(self.rows, key=lambda row: row.potential, default=None)

    @property
    def biggest_dividend(self):
        """The team handed the most by the other managers' lineups."""
        return max(self.rows, key=lambda row: row.delta, default=None)


# --------------------------------------------------------------------------
# Which league, and which week
# --------------------------------------------------------------------------


def league_id_for_guild(bot, guild_id: int | None) -> str:
    """The Fantrax league id for this server's current season.

    Raises :class:`MaxPfError` with something an admin can act on, because every
    way this fails is a configuration answer rather than a bug: no league bound,
    a league on another platform, or a season with no id on file.
    """
    ratings = ratings_config(bot, guild_id)
    if not ratings:
        raise MaxPfError(
            "This server isn't bound to a league, so there's nothing to report "
            "on. A commissioner can bind one with `/commish load`."
        )
    year = ratings.get("current_sports_year")
    platform = season_platform(ratings, year)
    if platform != PLATFORM:
        raise MaxPfError(
            f"The max-PF report reads {PLATFORM.title()} leagues, and "
            f"{year} ran on {platform or 'no platform on file'}."
        )
    league_id = ((ratings.get("seasons") or {}).get(year) or {}).get("league_id")
    if not league_id:
        raise MaxPfError(
            f"Season {year} has no {PLATFORM.title()} league id on file. "
            "`/commish season add` records one."
        )
    return str(league_id)


def latest_finished_period(
    platform, today: date, window: timedelta | None = None
) -> int | None:
    """The week to report on: the last one that finished, recently, with results.

    Three conditions, and each one was a wrong answer first:

    * **finished by the calendar.** Worked out from the league's own schedule
      rather than counting back seven days -- matchup periods are not all a week
      (an all-star break, a playoff round), and a period that is still being
      played would have a Tuesday lunchtime scoreline called final.
    * **it has results.** max-pf reads the regular season (its Fantrax adapter
      asks for ``playoffs=False``), so a playoff period is on the schedule with
      nothing behind it and would report a table of zeroes.
    * **recent**, when a ``window`` is given. Once the season is over the last
      regular-season week stays "the last finished period" forever, so the
      weekly process passes :data:`REPORT_WINDOW` and goes quiet in the
      offseason. A *query* passes nothing: somebody asking in July for the
      season to date should be told how it ended, not that it is July.
    """
    dates = platform.scoring_dates()
    teams = platform.team_ids()
    for period in sorted(platform.matchup_periods(), reverse=True):
        days = [dates[day] for day in platform.matchup_period_days(period) if day in dates]
        if not days:
            continue
        ended = max(days)
        if ended >= today or (window is not None and today - ended > window):
            continue
        if any(platform.matchup_opponent(team, period) is not None for team in teams):
            return period
    return None


def local_today(timezone: str) -> date:
    """Today, where the league lives -- 5am Monday in New York is not Monday in UTC."""
    return datetime.now(resolve_zone(timezone)).date()


def guild_today(bot, guild_id: int | None) -> date:
    """Today in this server's timezone, which is what decides "finished"."""
    from .tasks import resolve_timezone

    return local_today(resolve_timezone(bot, guild_id))


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------


def weekly_report(
    league_id: str, today: date, league_name: str, window: timedelta = REPORT_WINDOW
) -> Report | None:
    """The last finished period, if it finished recently. What the process posts.

    None when there is nothing to report -- no period has finished, the last one
    to finish is older than ``window`` (the offseason), or nobody played it.
    """
    platform = _open(league_id)
    period = latest_finished_period(platform, today, window)
    if period is None:
        return None
    return _report(league_id, week=period, through=period, league_name=league_name)


def query_report(
    league_id: str, today: date, league_name: str, *, week: int | None = None
) -> Report | None:
    """The season to date, or one named week. What a ``/elo maxpf`` query asks for.

    No recency window, unlike :func:`weekly_report`: a question asked in July
    about how the season went deserves an answer.

    Raises:
        MaxPfError: when ``week`` isn't a period this league has finished --
            with the range that is, since the alternative is somebody guessing.
    """
    platform = _open(league_id)
    through = latest_finished_period(platform, today)
    if through is None:
        return None
    if week is not None:
        week = _check_week(platform, week, today)
    return _report(league_id, week=week, through=through, league_name=league_name)


def _open(league_id: str):
    """The max-pf platform adapter for a Fantrax league id."""
    import max_pf

    return max_pf.build_platform({"platform": PLATFORM, "league_id": league_id})


def _check_week(platform, week: int, today: date) -> int:
    """``week``, or :class:`MaxPfError` saying why it can't be reported on."""
    periods = platform.matchup_periods()
    if week not in periods:
        raise MaxPfError(
            f"Week {week} isn't in this league's schedule — it runs "
            f"{min(periods)}–{max(periods)}."
        )
    dates = platform.scoring_dates()
    days = [dates[day] for day in platform.matchup_period_days(week) if day in dates]
    if days and max(days) >= today:
        # Reporting a week still being played would call a Tuesday lunchtime
        # scoreline final, and a max-PF ceiling on half a week is meaningless.
        raise MaxPfError(f"Week {week} isn't finished yet — it runs to {max(days)}.")
    if not any(
        platform.matchup_opponent(team, week) is not None for team in platform.team_ids()
    ):
        raise MaxPfError(
            f"Week {week} has no regular-season results — the metric reads the "
            "regular season, so a playoff week has nothing behind it."
        )
    return week


def _report(
    league_id: str, *, week: int | None, through: int, league_name: str
) -> Report | None:
    """Run max-pf and wrap the rows. Blocking: it scrapes and optimises.

    ``week=None`` reports every period the platform has results for -- the
    season to date. Periods with no matchup cost nothing (max-pf skips a team
    with no opponent before it fetches anything), so handing it the whole
    schedule is how "to date" is expressed.

    Call it through :func:`asyncio.to_thread`; both callers do.
    """
    import max_pf

    rows = max_pf.run(
        {"platform": PLATFORM, "league_id": league_id},
        weeks=week,
        # See the module docstring for why each of these is what it is.
        methodology="hindsight",
        nash=False,
        cache_dir=str(CACHE_DIR),
    )
    # A team with no counted matchup -- a bye, or the week the playoffs start
    # for everyone else -- comes back with no periods and a row of zeroes. It
    # didn't play; listing it as having scored nothing against a ceiling of
    # nothing would read as the worst week anyone ever had. If none of them
    # played, there is no report to make.
    played = [row for row in rows if row.periods]
    if not played:
        return None
    return Report(rows=played, league_name=league_name, week=week, through=through)


def render_report(report: Report) -> str:
    """The report as a Discord embed body: a headline or two, then the table.

    Not ``max_pf.render_markdown``: the package writes a Markdown pipe table,
    which is right for a file and wrong for Discord -- Discord renders no table
    syntax at all, so the pipes arrive as pipes. A monospace block is the format
    that survives, and the numbers in it are the package's own either way.
    """
    when = "over the season so far" if report.season else "this week"
    lines = []
    worst = report.most_left_on_the_table
    if worst is not None and worst.potential < 0:
        lines.append(
            f"**{worst.name}** left the most on the table {when}: "
            f"{abs(worst.potential):.1f} category points "
            f"({worst.actual_pf:.1f} scored, {worst.m1_pf:.1f} available)."
        )
    gifted = report.biggest_dividend
    if gifted is not None and gifted.delta > 0:
        lines.append(
            f"**{gifted.name}** was handed the most by the other bench: "
            f"Δ {gifted.delta:.1f}."
        )
    lines.append("```")
    lines.extend(_table(report.rows, season=report.season))
    lines.append("```")
    key = "*Actual* scored · *M1* best lineup vs their actual · " \
          "*Pot* points left on the table · *Δ* their mismanagement"
    if report.season:
        key = "*GP* weeks played · " + key
    lines.append(key)
    return "\n".join(lines)


def _table(rows: list, *, season: bool = False) -> list[str]:
    """A fixed-width table, narrow enough to read on a phone.

    A season report carries **GP**: the rows are sums, so a team that missed a
    week would otherwise look worse than one that played every one of them.
    """
    shown = rows[:MAX_ROWS]
    header = f"{'Team':<{MAX_NAME}}"
    if season:
        header += f" {'GP':>3}"
    header += f" {'Actual':>6} {'M1':>6} {'Pot':>6} {'Δ':>5}"
    out = [header, "-" * len(header)]
    for row in shown:
        name = row.name if len(row.name) <= MAX_NAME else row.name[: MAX_NAME - 1] + "…"
        line = f"{name:<{MAX_NAME}}"
        if season:
            line += f" {row.periods:>3}"
        line += (
            f" {row.actual_pf:>6.1f} {row.m1_pf:>6.1f} "
            f"{row.potential:>+6.1f} {row.delta:>5.1f}"
        )
        out.append(line)
    if len(rows) > MAX_ROWS:
        out.append(f"…and {len(rows) - MAX_ROWS} more.")
    return out


async def weekly_maxpf(bot, task) -> ProcessOutcome:
    """Report the last finished matchup period's max-PF table."""
    try:
        league_id = league_id_for_guild(bot, task.guild_id)
    except MaxPfError as exc:
        # A configuration answer, so it goes on the row as the reason rather
        # than into the channel as a report nobody can act on.
        raise TaskError(str(exc)) from None

    ratings = ratings_config(bot, task.guild_id) or {}
    league_name = ratings.get("league_name") or "The league"
    report = await asyncio.to_thread(
        weekly_report, league_id, local_today(task.timezone), league_name
    )

    if report is None:
        return ProcessOutcome(
            title=f"{league_name} — no finished week",
            body="No matchup period has finished yet, so there's nothing to report.",
            announce=False,
        )
    return ProcessOutcome(title=report.title, body=render_report(report))


def maxpf_process() -> WeeklyProcess:
    """The max-PF process, for the max-PF cog to register on load."""
    return WeeklyProcess(
        name=PROCESS_MAXPF,
        summary=(
            "The week's max points for: what every team scored against what its "
            "best lineup could have."
        ),
        run=weekly_maxpf,
        sports=("basketball",),
    )
