"""Turning a league into a dynasty, and back.

A dynasty league rates every season on one continuous timeline: each season's
weeks are offsets into a single ``dynasty_elo`` frame, and week 0 of a new season
carries the last one's ratings over (regressed toward the mean) instead of
starting everybody at 1500. Whether a league is one is ``is_dynasty`` in its
ratings config -- a property of the league, not of the Discord server, which is
why it lives here and under ``/commish`` rather than in ``/setup``.

The conversion itself is elo_system's :meth:`EloLeague.change_dynasty`: it
scrapes every season, works out where each one starts on the timeline, and
replays them all into the dynasty frame. This module wraps it for the bot, for
two reasons the package can't see from where it sits:

**The current season's length is the schedule, not what has been played.** On
Fantrax ``current_season_length`` counts every period on the schedule, so a
season that has only just started reports all of them -- and the replay rates
``0:current_season_length`` for every season, which would invent a result for
each week nobody has played yet. So for the duration of the conversion the
current season's length is held at the last week its own ratings reach, and put
back afterwards.

**The package declines quietly.** ``change_dynasty`` returns the flag rather than
raising when a league can't be converted, so the reasons are checked here first,
where they can still be named to whoever asked.

Everything here is blocking (it scrapes a platform); call it through
:func:`asyncio.to_thread`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from elo_system.tools.basics import WEEK_STR


@dataclass
class DynastyResult:
    """Where a league stands after a change to its dynasty setting."""

    enabled: bool
    # season -> its week 0 on the dynasty timeline
    start_weeks: dict = field(default_factory=dict)
    # The last week on the dynasty timeline that holds ratings, if there is one.
    last_week: int | None = None
    # True when the league had no ratings yet, so there was nothing to replay
    # and only the flag was set.
    flag_only: bool = False


def last_seasonal_week(frame) -> int:
    """The last week of a season frame rated without a gap; 0 with no frame."""
    if frame is None:
        return 0
    week = 0
    while frame.get(WEEK_STR.format(week + 1)) is not None:
        week += 1
    return week


def last_dynasty_week(frame) -> int | None:
    """The last ``week_N`` column of the dynasty frame, or None without one."""
    if frame is None:
        return None
    weeks = [
        int(str(column)[len("week_"):])
        for column in frame.columns
        if str(column).startswith("week_") and str(column)[len("week_"):].isdigit()
    ]
    return max(weeks) if weeks else None


def _missing_years(seasons) -> list[int]:
    """Years absent between the first and last configured season."""
    years = sorted(seasons)
    if not years:
        return []
    return [y for y in range(years[0], years[-1] + 1) if y not in seasons]


def _prepare(system):
    league = system.elo_league
    if league is None:
        raise RuntimeError("League config not loaded")
    if not league.loaded:
        league.load()
    # The ratings on file decide whether there is anything to convert, so they
    # have to be in memory before the question is asked.
    if league.frame_manager is None:
        system.load_frames(None)
    return league


def _result(league, *, flag_only: bool = False) -> DynastyResult:
    fm = league.frame_manager
    starts = {
        year: season["dynasty_start_week"]
        for year, season in sorted(league.seasons.items())
        if league.is_dynasty and season.get("dynasty_start_week") is not None
    }
    return DynastyResult(
        enabled=bool(league.is_dynasty),
        start_weeks=starts,
        last_week=last_dynasty_week(fm.dynasty_elo) if fm is not None else None,
        flag_only=flag_only,
    )


def _save(system) -> None:
    """Write the league's config (and its frames) back to where they came from."""
    # dump() folds the instance state -- is_dynasty above all -- back into
    # system.ratings_config, which write_configs then puts on disk.
    system.elo_league.dump()
    system.write_configs("ratings")


def status(system) -> DynastyResult:
    """The league's dynasty setting, without changing anything."""
    return _result(_prepare(system))


def enable(system) -> DynastyResult:
    """Convert the league to a dynasty, replaying its seasons onto one timeline.

    Raises ``ValueError`` for anything the commissioner can fix -- already a
    dynasty, a gap between seasons -- so the cog can say what it was.
    """
    league = _prepare(system)
    if league.is_dynasty:
        raise ValueError("This league is already a dynasty.")
    if not league.seasons:
        raise ValueError("This league has no seasons configured yet.")
    if missing := _missing_years(league.seasons):
        raise ValueError(
            "A dynasty runs its seasons back to back, but "
            + ", ".join(str(y) for y in missing)
            + " isn't configured. Add the missing season(s) first."
        )

    fm = league.frame_manager
    # The frame manager keeps its own copy of the flag, and the package refuses
    # to convert a league whose frame manager already thinks it is a dynasty.
    # The league's flag is the one on file, so the manager is brought into line.
    fm.set_is_dynasty(False)

    if not fm.has_data():
        # Nothing rated yet, so nothing to replay: the package's advice is to
        # build it as a dynasty from the start, which is just the flag -- the
        # run pipeline lays the dynasty frame down as weeks get rated.
        league.is_dynasty = True
        fm.set_is_dynasty(True)
        _save(system)
        return _result(league, flag_only=True)

    # Scrape every season up front. The package would do it inside the
    # conversion, but a scrape resets a season's length to the platform's, and
    # the current season's has to be held down (below) after that has happened.
    for year in sorted(league.seasons):
        league.add_league(year)

    current = league.current_sports_year
    # The package seeds the dynasty frame from the first season frame it
    # generates during the conversion. If that is a season it had to rate
    # first, rather than the earliest, the timeline starts from the wrong
    # season's members. So every season gets a frame of its own beforehand,
    # while the frame manager still rates seasons on their own: past seasons
    # are played out and rated in full, the current one is seeded at week 0.
    for year in sorted(league.seasons):
        if year == current:
            fm.generate(year, overwrite=False)
        elif not fm.validate_season(year):
            league.run_season(year)


    season = league.seasons.get(current)
    original = None
    if season is not None:
        original = season.get("current_season_length")
        rated = last_seasonal_week(fm.seasonal_elo.get(current))
        if original is None or rated < original:
            season["current_season_length"] = rated
    try:
        league.change_dynasty(True)
    finally:
        if season is not None:
            if original is None:
                season.pop("current_season_length", None)
            else:
                season["current_season_length"] = original

    if not league.is_dynasty:
        # Every reason the package checks was checked above; landing here means
        # one it has since grown, so say so rather than report a success.
        raise RuntimeError("elo_system declined to convert this league.")

    system.publish()
    _save(system)
    return _result(league)


def disable(system) -> DynastyResult:
    """Go back to rating each season on its own.

    The dynasty frame already on file is left alone -- it simply stops being
    written -- so switching back on later replays onto it rather than from
    scratch.
    """
    league = _prepare(system)
    if not league.is_dynasty:
        raise ValueError("This league isn't a dynasty.")
    league.change_dynasty(False)
    league.frame_manager.set_is_dynasty(False)
    _save(system)
    return _result(league)
