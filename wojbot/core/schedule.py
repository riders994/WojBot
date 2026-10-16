"""When a scheduled task fires: the recurrence rules and the phrase parser.

Pure domain code -- no Discord, no database, no clock of its own. Every function
here takes the current time as an argument, which is what makes the whole module
testable and what keeps "what does 'weekly on monday at 9am' mean" in one place
rather than spread across a cog.

Three kinds of schedule, and the kind is stored alongside its spec so a row can
be read back into the same object (:func:`parse_stored`):

* :class:`OneShot` -- fires once, at an instant. Exhausted afterwards.
* :class:`Interval` -- fires every N, anchored to a first firing so a task can
  never drift: the anchor and the period are both stored, and the next firing is
  computed from the anchor rather than from when the last one happened to land.
* :class:`Cron` -- fires on a calendar ("weekly on monday at 9am"), in a named
  timezone.

**Wall-clock times are local, instants are UTC.** A :class:`Cron` keeps its
timezone and re-resolves the offset on every firing, so a 9am reminder is still
9am on the other side of a daylight-saving change -- storing the next firing as
UTC and adding 7 days to it would quietly become 8am or 10am for half the year.
:class:`Interval` deliberately does *not* work that way: "every 12 hours" means
twelve elapsed hours, not the same wall-clock time, so its arithmetic is pure
UTC.

That difference is why the two have different keywords rather than sharing one.
**``every`` takes a number** -- ``every 3d``, ``every 12h`` -- and builds an
:class:`Interval`. A calendar repeat is ``daily``, ``weekly on monday``,
``weekdays``, ``weekends``, ``monthly`` or ``hourly``, and builds a
:class:`Cron`. When ``every`` accepted both, the word gave no hint which of the
two behaviours you were getting.

The parser (:func:`parse_schedule`) accepts the phrases a manager would actually
type. It is strict about what it does not understand -- :class:`ScheduleError`
carries a message meant to be shown to whoever typed it, listing the forms that
do work, because a schedule that was silently misread doesn't fail until the
moment it was supposed to fire.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

UTC = timezone.utc

# A recurrence may not be tighter than this. The runner ticks on a coarser
# schedule than a second, and anything under a minute would be a task that fires
# on some ticks and not others -- worse than refusing it.
MIN_INTERVAL = timedelta(minutes=1)

# How far ahead a one-shot may be set. Not a technical limit; a five-year
# reminder is far more likely to be a typo in a year than a real intention.
MAX_HORIZON = timedelta(days=730)

# How far :meth:`Cron.next_fire_after` will search before giving up. Four years
# covers the sparsest legal expression (Feb 29 falling on a given weekday needs
# rather longer, and is the one case that legitimately returns None).
_CRON_SEARCH_DAYS = 366 * 4 + 1

WEEKDAY_NAMES = {
    "sunday": 0, "sun": 0,
    "monday": 1, "mon": 1,
    "tuesday": 2, "tue": 2, "tues": 2,
    "wednesday": 3, "wed": 3,
    "thursday": 4, "thu": 4, "thur": 4, "thurs": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}

MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "week": 604800, "weeks": 604800,
}

# Cron field order, with the legal range for each. Weekday is 0-7 on the way in
# because both 0 and 7 mean Sunday in every crontab; 7 is folded to 0 on parse.
_CRON_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)

# Accepted phrases, quoted back when one isn't understood. This list is the
# user-facing documentation of the parser, so it belongs next to it.
ACCEPTED_FORMS = (
    "`in 90m`, `in 2h30m`, `in 3 days`",
    "`9am`, `21:30`, `noon` (today if it's still to come, else tomorrow)",
    "`tomorrow 8am`, `friday 8pm`, `sep 1 at 9am`, `2026-09-01 14:00`",
    # Two kinds of repeat, deliberately two different words. `every` is elapsed
    # time; the calendar words hold a wall-clock time across a DST change.
    "`every 3d`, `every 12h` — a repeat every N (`every` takes a number)",
    "`daily at 9am`, `weekly on monday at 9am`, `hourly`",
    "`weekdays at 8:30am`, `weekends at 10am`, `monthly on the 1st at 9am`",
    "`cron 0 9 * * MON` (minute hour day month weekday)",
)


class ScheduleError(ValueError):
    """A schedule that can't be read, or can't be honoured. Message is for the user."""


def resolve_zone(name: str) -> ZoneInfo:
    """The :class:`ZoneInfo` for ``name``, or :class:`ScheduleError` if it isn't one.

    Raising rather than falling back to UTC on purpose: a task set for 9am in a
    timezone the host has never heard of should say so at the moment it is set,
    not fire six hours off and leave nobody able to explain why.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ScheduleError(
            f"`{name}` isn't a timezone I know. Use an IANA name like "
            "`America/New_York`, `Europe/London` or `UTC`."
        ) from exc


def available_zones() -> list[str]:
    """Every IANA zone this host knows, sorted. Cached -- the list never moves.

    Behind a function rather than a module constant because building it touches
    the filesystem (or the tzdata package), and a bot that never sets a reminder
    should not pay for it at import.
    """
    global _ZONE_CACHE
    if _ZONE_CACHE is None:
        _ZONE_CACHE = sorted(available_timezones())
    return _ZONE_CACHE


_ZONE_CACHE: list[str] | None = None


def _to_utc(moment: datetime, zone: ZoneInfo) -> datetime:
    """Read a naive local wall-clock time in ``zone`` and return the UTC instant.

    ``fold=0`` is what makes an ambiguous local time -- the hour that repeats
    when the clocks go back -- resolve to its *first* occurrence, so the task
    fires once rather than twice. A local time that does not exist at all (the
    hour skipped when the clocks go forward) is resolved by Python using the
    offset in force before the transition, which lands it an hour later that
    same day; that is the least surprising of the available wrong answers.
    """
    return moment.replace(tzinfo=zone, fold=0).astimezone(UTC)


# --------------------------------------------------------------------------
# The three schedule kinds
# --------------------------------------------------------------------------


class Schedule(ABC):
    """A rule for when something fires.

    Subclasses are frozen dataclasses, so a schedule can be compared and
    hashed, and they round-trip through ``(kind, spec, timezone)`` -- the three
    columns a stored task keeps.
    """

    kind: str

    @abstractmethod
    def next_fire_after(self, now: datetime) -> datetime | None:
        """The first firing strictly after ``now``, or None if there is none.

        ``now`` must be timezone-aware. None means exhausted: a one-shot that
        has already fired, or a calendar expression with no future match.
        """

    @property
    @abstractmethod
    def spec(self) -> str:
        """The stored form, readable back by :func:`parse_stored`."""

    @abstractmethod
    def describe(self, zone: ZoneInfo) -> str:
        """How to say this schedule to somebody, in their timezone."""

    @property
    def recurring(self) -> bool:
        """Whether this schedule can fire more than once."""
        return True


@dataclass(frozen=True)
class OneShot(Schedule):
    """Fires once, at :attr:`when`."""

    kind = "once"
    when: datetime

    def __post_init__(self) -> None:
        if self.when.tzinfo is None:
            raise ScheduleError("A one-shot schedule needs an aware datetime.")

    def next_fire_after(self, now: datetime) -> datetime | None:
        return self.when if self.when > now else None

    @property
    def spec(self) -> str:
        return self.when.astimezone(UTC).isoformat()

    def describe(self, zone: ZoneInfo) -> str:
        local = self.when.astimezone(zone)
        return f"once, on {local:%a %d %b %Y at %H:%M} ({local:%Z})"

    @property
    def recurring(self) -> bool:
        return False


@dataclass(frozen=True)
class Interval(Schedule):
    """Fires every :attr:`every`, counted from :attr:`anchor`.

    Both halves are stored because the next firing is computed from the anchor,
    not from the previous firing. A task whose delivery was late -- the bot was
    down, the tick was busy -- otherwise adds that lateness to every subsequent
    firing, and a daily reminder walks slowly around the clock.
    """

    kind = "interval"
    every: timedelta
    anchor: datetime

    def __post_init__(self) -> None:
        if self.anchor.tzinfo is None:
            raise ScheduleError("An interval schedule needs an aware anchor.")
        if self.every < MIN_INTERVAL:
            raise ScheduleError(
                f"The shortest repeat I'll take is every {_describe_delta(MIN_INTERVAL)}."
            )

    def next_fire_after(self, now: datetime) -> datetime | None:
        if now < self.anchor:
            return self.anchor
        # How many whole periods have elapsed since the anchor, plus one. Integer
        # division on timedeltas, so this is exact rather than a loop.
        elapsed = now - self.anchor
        steps = elapsed // self.every + 1
        return self.anchor + steps * self.every

    @property
    def spec(self) -> str:
        return f"{self.anchor.astimezone(UTC).isoformat()}/{int(self.every.total_seconds())}"

    def describe(self, zone: ZoneInfo) -> str:
        local = self.anchor.astimezone(zone)
        return (
            f"every {_describe_delta(self.every)}, "
            f"from {local:%a %d %b at %H:%M} ({local:%Z})"
        )


@dataclass(frozen=True)
class Cron(Schedule):
    """Fires on a calendar, in :attr:`tz`.

    Each field is a frozenset of the values it matches, or None for ``*``.
    Weekdays are 0-6 with **0 = Sunday**, as in a crontab.

    Day-of-month and day-of-week are OR'd when both are restricted, which is
    crontab's own rule: ``0 9 13 * FRI`` means the 13th *and* every Friday, not
    Friday the 13th. Surprising, but matching the thing it is named after beats
    being quietly different from it.
    """

    kind = "cron"
    minute: frozenset[int] | None
    hour: frozenset[int] | None
    day: frozenset[int] | None
    month: frozenset[int] | None
    weekday: frozenset[int] | None
    tz: str = "UTC"

    def next_fire_after(self, now: datetime) -> datetime | None:
        zone = resolve_zone(self.tz)
        local = now.astimezone(zone)
        minutes = sorted(self.minute) if self.minute is not None else range(60)
        hours = sorted(self.hour) if self.hour is not None else range(24)

        # Day by day rather than minute by minute: a sparse expression can be
        # years away, and 2M candidate minutes to reach it is not a search.
        for offset in range(_CRON_SEARCH_DAYS):
            day = local.date() + timedelta(days=offset)
            if not self._day_matches(day):
                continue
            for hour in hours:
                for minute in minutes:
                    # Ascending within the day, so the first candidate past
                    # `now` is the earliest one.
                    moment = _to_utc(datetime.combine(day, time(hour, minute)), zone)
                    if moment > now:
                        return moment
        return None

    def _day_matches(self, day: date) -> bool:
        if self.month is not None and day.month not in self.month:
            return False
        # date.isoweekday() is 1=Monday..7=Sunday; cron is 0=Sunday..6=Saturday.
        weekday = day.isoweekday() % 7
        day_restricted = self.day is not None
        weekday_restricted = self.weekday is not None
        if day_restricted and weekday_restricted:
            return day.day in self.day or weekday in self.weekday
        if day_restricted:
            return day.day in self.day
        if weekday_restricted:
            return weekday in self.weekday
        return True

    @property
    def spec(self) -> str:
        return " ".join(
            _render_cron_field(getattr(self, name))
            for name, _, _ in _CRON_FIELDS
        )

    def describe(self, zone: ZoneInfo) -> str:
        del zone  # a cron carries its own; showing it in another would mislead
        return f"`{self.spec}` ({self.tz})"


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


def _render_cron_field(values: frozenset[int] | None) -> str:
    return "*" if values is None else ",".join(str(v) for v in sorted(values))


def _describe_delta(delta: timedelta) -> str:
    """``timedelta(minutes=90)`` -> ``"1h30m"``; used in confirmations."""
    total = int(delta.total_seconds())
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, total = divmod(total, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or "0s"


# --------------------------------------------------------------------------
# Parsing: durations and times
# --------------------------------------------------------------------------

_DURATION_PART = re.compile(r"(\d+)\s*([a-z]+)")


def parse_duration(text: str) -> timedelta:
    """``"2h30m"``, ``"90 minutes"``, ``"3 days"`` -> a :class:`timedelta`.

    Raises:
        ScheduleError: if nothing in it reads as a duration, or a unit is not
            one of seconds/minutes/hours/days/weeks.
    """
    text = text.strip().lower()
    if not text:
        raise ScheduleError("How long? Try `90m`, `2h30m` or `3 days`.")
    seconds = 0
    matched = 0
    for count, unit in _DURATION_PART.findall(text):
        if unit not in _UNIT_SECONDS:
            raise ScheduleError(
                f"`{unit}` isn't a length of time I know. Use seconds, minutes, "
                "hours, days or weeks."
            )
        seconds += int(count) * _UNIT_SECONDS[unit]
        matched += 1
    if not matched:
        raise ScheduleError(f"`{text}` isn't a length of time. Try `90m` or `2h30m`.")
    # A bare number with no unit ("in 5") gets caught here rather than being
    # guessed at: five what?
    if _DURATION_PART.sub("", text).strip(" ,and"):
        raise ScheduleError(
            f"I couldn't read all of `{text}` as a length of time. Try `90m` or `2h30m`."
        )
    if seconds <= 0:
        raise ScheduleError("That length of time is zero.")
    return timedelta(seconds=seconds)


_TIME_PATTERNS = (
    # 9am, 9 am, 9:30pm, 09:30, 21:30, 9.30am
    re.compile(r"^(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)$"),
    re.compile(r"^(?P<hour>\d{1,2})[:.](?P<minute>\d{2})$"),
)


def parse_time_of_day(text: str) -> time:
    """``"9am"``, ``"21:30"``, ``"noon"``, ``"midnight"`` -> a :class:`~datetime.time`.

    A bare hour is only accepted with a meridiem or a colon: ``9`` on its own is
    as likely to be a day of the month as an hour, and guessing at it is how a
    reminder ends up twelve hours out.
    """
    text = text.strip().lower().replace(" ", "")
    if text in ("noon", "midday"):
        return time(12, 0)
    if text == "midnight":
        return time(0, 0)
    for pattern in _TIME_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        hour = int(match["hour"])
        minute = int(match["minute"] or 0)
        meridiem = match.groupdict().get("meridiem")
        if meridiem:
            if not 1 <= hour <= 12:
                raise ScheduleError(f"`{text}` isn't a time — an hour with am/pm is 1-12.")
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        if hour > 23 or minute > 59:
            raise ScheduleError(f"`{text}` isn't a time of day.")
        return time(hour, minute)
    raise ScheduleError(
        f"`{text}` isn't a time of day. Try `9am`, `9:30pm`, `21:30` or `noon`."
    )


# --------------------------------------------------------------------------
# Parsing: cron fields
# --------------------------------------------------------------------------


def _parse_cron_field(
    text: str, name: str, low: int, high: int
) -> frozenset[int] | None:
    """One crontab field -> the values it matches, or None for ``*``.

    Handles ``*``, ``*/n``, ``a``, ``a-b``, ``a-b/n`` and comma-separated lists
    of those, plus three-letter names in the month and weekday fields.
    """
    text = text.strip().lower()
    if not text:
        raise ScheduleError(f"The {name} field of a cron expression is empty.")
    if text == "*":
        return None

    names = MONTH_NAMES if name == "month" else WEEKDAY_NAMES if name == "weekday" else {}

    def one(token: str) -> int:
        if token in names:
            return names[token]
        try:
            value = int(token)
        except ValueError:
            raise ScheduleError(
                f"`{token}` isn't a {name} I can read."
            ) from None
        if not low <= value <= high:
            raise ScheduleError(f"{name} must be {low}-{high}, got `{value}`.")
        return value

    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise ScheduleError(f"`{step_text}` isn't a step for {name}.") from None
            if step < 1:
                raise ScheduleError(f"A {name} step must be 1 or more.")
            part = part or "*"
        if part == "*":
            start, end = low, high
        elif "-" in part.lstrip("-"):
            start_text, _, end_text = part.partition("-")
            start, end = one(start_text), one(end_text)
            if start > end:
                raise ScheduleError(f"`{part}` runs backwards for {name}.")
        else:
            start = end = one(part)
        values.update(range(start, end + 1, step))

    if name == "weekday":
        # Both 0 and 7 are Sunday in a crontab; store the one _day_matches uses.
        values = {0 if value == 7 else value for value in values}
    if not values:
        raise ScheduleError(f"`{text}` matches no {name}.")
    return frozenset(values)


def parse_cron(expression: str, tz: str = "UTC") -> Cron:
    """A five-field crontab expression -> a :class:`Cron`."""
    fields = expression.split()
    if len(fields) != 5:
        raise ScheduleError(
            f"A cron expression has five fields (minute hour day month weekday); "
            f"`{expression.strip()}` has {len(fields)}."
        )
    parsed = {
        name: _parse_cron_field(field, name, low, high)
        for field, (name, low, high) in zip(fields, _CRON_FIELDS)
    }
    return Cron(tz=tz, **parsed)


# --------------------------------------------------------------------------
# Parsing: the phrases people type
# --------------------------------------------------------------------------

# Longest first, so "sunday" is matched before "sun" leaves "day" behind.
_WEEKDAY_WORDS = "|".join(sorted(WEEKDAY_NAMES, key=len, reverse=True))
_MONTH_WORDS = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
# One weekday token, plural allowed: "weekly on mondays and fridays" is a thing
# people type, and the plural belongs on each name rather than after the list.
_WEEKDAY_TOKEN = rf"(?:{_WEEKDAY_WORDS})s?"
_AT = r"(?:\s+at)?\s+"

_IN = re.compile(r"^in\s+(?P<duration>.+)$")

# "every" is the interval keyword and takes a length of time, nothing else.
# Every calendar recurrence has its own word -- see the note above _match for
# why the two are kept apart.
_EVERY = re.compile(r"^every\s+(?P<duration>.+)$")

# The calendar words. Each one fixes a different cron field, and each takes an
# optional time of day that defaults to DEFAULT_TIME.
_HOURLY = re.compile(r"^hourly$")
_DAILY = re.compile(rf"^daily(?:{_AT}(?P<time>.+))?$")
_WEEKLY = re.compile(
    rf"^weekly(?:\s+on)?\s+(?P<days>{_WEEKDAY_TOKEN}"
    rf"(?:\s*(?:,|and|&)\s*{_WEEKDAY_TOKEN})*)(?:{_AT}(?P<time>.+))?$"
)
# "weekly" with no day after it, so the error can say which bit is missing.
_BARE_WEEKLY = re.compile(rf"^weekly(?:\s+on)?(?:{_AT}(?P<time>.+))?$")
_WEEKDAYS = re.compile(rf"^(?:weekday|workday)s?(?:{_AT}(?P<time>.+))?$")
_WEEKENDS = re.compile(rf"^weekends?(?:{_AT}(?P<time>.+))?$")
_MONTHLY = re.compile(
    rf"^monthly(?:\s+on)?(?:\s+the)?\s*"
    rf"(?P<day>\d{{1,2}})?(?:st|nd|rd|th)?(?:{_AT}(?P<time>.+))?$"
)
_CRON = re.compile(r"^cron\s+(?P<expression>.+)$")
_TOMORROW = re.compile(rf"^tomorrow(?:{_AT}(?P<time>.+))?$")
_TODAY = re.compile(rf"^today(?:{_AT}(?P<time>.+))?$")
_NEXT_WEEKDAY = re.compile(
    rf"^(?:next\s+|this\s+|on\s+)?(?P<day>{_WEEKDAY_WORDS})(?:{_AT}(?P<time>.+))?$"
)
_MONTH_DAY = re.compile(
    rf"^(?P<month>{_MONTH_WORDS})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s+(?P<year>\d{{4}}))?(?:{_AT}(?P<time>.+))?$"
)
_DAY_MONTH = re.compile(
    rf"^(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>{_MONTH_WORDS})"
    rf"(?:,?\s+(?P<year>\d{{4}}))?(?:{_AT}(?P<time>.+))?$"
)
_ISO_DATE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:[\sT]+(?P<time>.+))?$"
)

# What a bare time means when no date is given, and the default for a date
# given without one. 9am rather than midnight: a reminder set for "friday" is
# meant to be seen on Friday, and nobody reads Discord at midnight.
DEFAULT_TIME = time(9, 0)


# What to suggest when "every" is handed a calendar word instead of a number.
# The rewrite is spelled out rather than gestured at: somebody who typed "every
# monday at 9am" wants the phrase that does what they meant, not a grammar
# lesson. Ordered so the specific words win -- "weekday" must not be read as
# "week", which the \b in each pattern is what guarantees.
_EVERY_REWRITES = (
    (re.compile(r"^(?:weekday|workday)s?\b(?P<rest>.*)$"), "weekdays{rest}"),
    (re.compile(r"^weekends?\b(?P<rest>.*)$"), "weekends{rest}"),
    (re.compile(rf"^(?P<day>{_WEEKDAY_WORDS})s?\b(?P<rest>.*)$"), "weekly on {day}{rest}"),
    (re.compile(r"^days?\b(?P<rest>.*)$"), "daily{rest}"),
    # A bare "every week" never said which day, so the suggestion keeps the
    # placeholder rather than quietly deciding Monday on their behalf.
    (re.compile(r"^weeks?\b(?P<rest>.*)$"), "weekly on <day>{rest}"),
    (re.compile(r"^months?\b(?P<rest>.*)$"), "monthly{rest}"),
    (re.compile(r"^hours?\b(?P<rest>.*)$"), "hourly"),
)

CALENDAR_FORMS = (
    "`daily at 9am`",
    "`weekly on monday at 9am`",
    "`weekdays at 8:30am`",
    "`weekends at 10am`",
    "`monthly on the 1st at 9am`",
    "`hourly`",
)


def _interval(duration: str, *, now: datetime) -> Interval:
    """``"3d"`` -> an :class:`Interval`, anchored one period out.

    Raises:
        ScheduleError: if ``duration`` isn't a length of time. "every" takes a
            number and nothing else, so the message's job is to hand back the
            calendar phrasing the caller almost certainly wanted instead.
    """
    try:
        every = parse_duration(duration)
    except ScheduleError:
        raise ScheduleError(_every_needs_a_number(duration)) from None
    # Not caught: a duration that parsed but is shorter than MIN_INTERVAL. That
    # one knows exactly what is wrong with it and says so itself.
    return Interval(every=every, anchor=now + every)


def _every_needs_a_number(duration: str) -> str:
    """The message for an "every" that was handed a calendar word."""
    for pattern, rewrite in _EVERY_REWRITES:
        match = pattern.match(duration)
        if match:
            suggestion = rewrite.format(**match.groupdict(default=""))
            return (
                f"`every` takes a length of time, like `every 3d` or `every 12h`. "
                f"For a repeat on the calendar, drop the `every` — you want "
                f"`{suggestion.strip()}`."
            )
    forms = ", ".join(CALENDAR_FORMS)
    return (
        f"`every {duration}` — `every` takes a length of time, like `every 3d` or "
        f"`every 12h`. For a repeat on the calendar, use one of: {forms}."
    )


def _cron_at(when: time, tz: str, **fields) -> Cron:
    """A :class:`Cron` firing at one time of day, with the given day fields."""
    return Cron(
        minute=frozenset({when.minute}),
        hour=frozenset({when.hour}),
        day=fields.get("day"),
        month=fields.get("month"),
        weekday=fields.get("weekday"),
        tz=tz,
    )


def _time_or_default(text: str | None) -> time:
    return DEFAULT_TIME if not text else parse_time_of_day(text)


def _next_local(
    when: time, zone: ZoneInfo, now: datetime, *, weekday: int | None = None
) -> datetime:
    """The next local ``when`` (optionally on cron-weekday ``weekday``), as UTC."""
    local_now = now.astimezone(zone)
    for offset in range(8):
        day = local_now.date() + timedelta(days=offset)
        if weekday is not None and day.isoweekday() % 7 != weekday:
            continue
        moment = _to_utc(datetime.combine(day, when), zone)
        if moment > now:
            return moment
    # Unreachable: eight days covers every weekday plus today twice over.
    raise ScheduleError("Couldn't find the next occurrence of that time.")


def parse_schedule(text: str, *, now: datetime, tz: str = "UTC") -> Schedule:
    """Read a schedule out of what somebody typed.

    Args:
        text: the phrase -- see :data:`ACCEPTED_FORMS`.
        now: the instant to read relative phrases against. Must be aware.
        tz: IANA name for the timezone wall-clock times are in. Instants
            ("in 90m") don't use it; times of day and cron expressions do.

    Raises:
        ScheduleError: with a message for whoever typed it, when the phrase
            can't be read, names a timezone that doesn't exist, or resolves to
            a moment already past or absurdly far off.
    """
    if now.tzinfo is None:
        raise ScheduleError("parse_schedule needs an aware `now`.")
    zone = resolve_zone(tz)
    raw = " ".join((text or "").split())
    if not raw:
        raise ScheduleError(_unreadable(""))
    lowered = raw.lower()

    schedule = _match(lowered, now=now, tz=tz, zone=zone)
    if schedule is None:
        raise ScheduleError(_unreadable(raw))

    # Whatever it parsed as, it has to have a future firing -- otherwise this is
    # a schedule that is already over, and saying so now is the whole point.
    upcoming = schedule.next_fire_after(now)
    if upcoming is None:
        raise ScheduleError(
            f"`{raw}` has no next occurrence — it's already in the past."
        )
    if upcoming - now > MAX_HORIZON:
        raise ScheduleError(
            f"That's {(upcoming - now).days} days out, past the "
            f"{MAX_HORIZON.days}-day limit. If you meant it, set something nearer "
            "and move it later."
        )
    return schedule


def _match(
    lowered: str, *, now: datetime, tz: str, zone: ZoneInfo
) -> Schedule | None:
    """The first pattern that reads ``lowered``, or None.

    Order matters: the recurring forms are tried before the one-shot ones, so
    "friday 8pm" is not read as a bare time.

    **"every" means an interval and takes a number.** ``every 3d`` is elapsed
    time; a calendar repeat is ``daily``, ``weekly on monday``, ``weekdays``,
    ``monthly`` or ``hourly``. The two used to share the word, and sharing it hid
    the one distinction that matters most here: ``every 24h`` is twenty-four
    elapsed hours and drifts an hour across a daylight-saving change, while
    ``daily at 9am`` is 9am forever. Two behaviours behind one word is a thing
    nobody can be told once and remember, so they get two words.
    """
    match = _CRON.match(lowered)
    if match:
        return parse_cron(match["expression"], tz=tz)

    match = _IN.match(lowered)
    if match:
        return OneShot(now + parse_duration(match["duration"]))

    match = _EVERY.match(lowered)
    if match:
        return _interval(match["duration"], now=now)

    if _HOURLY.match(lowered):
        return Cron(
            minute=frozenset({0}), hour=None, day=None, month=None, weekday=None, tz=tz
        )

    match = _DAILY.match(lowered)
    if match:
        return _cron_at(_time_or_default(match["time"]), tz)

    match = _WEEKLY.match(lowered)
    if match:
        days = re.split(r"\s*(?:,|and|&)\s*", match["days"])
        weekdays = frozenset(WEEKDAY_NAMES[day.rstrip("s")] for day in days)
        return _cron_at(_time_or_default(match["time"]), tz, weekday=weekdays)

    if _BARE_WEEKLY.match(lowered):
        # The one calendar word that cannot stand alone: daily, monthly and
        # hourly all know when they mean, and "weekly" does not. Guessing a day
        # would be a reminder quietly landing on a day nobody chose.
        raise ScheduleError(
            "`weekly` needs a day — `weekly on monday at 9am`. (`daily`, "
            "`monthly` and `hourly` work on their own.)"
        )

    match = _WEEKDAYS.match(lowered)
    if match:
        return _cron_at(
            _time_or_default(match["time"]), tz, weekday=frozenset({1, 2, 3, 4, 5})
        )

    match = _WEEKENDS.match(lowered)
    if match:
        return _cron_at(
            _time_or_default(match["time"]), tz, weekday=frozenset({0, 6})
        )

    match = _MONTHLY.match(lowered)
    if match:
        day = int(match["day"] or 1)
        if not 1 <= day <= 31:
            raise ScheduleError(f"There's no {day}th of the month.")
        return _cron_at(
            _time_or_default(match["time"]), tz, day=frozenset({day})
        )

    match = _TOMORROW.match(lowered)
    if match:
        local = now.astimezone(zone)
        day = local.date() + timedelta(days=1)
        return OneShot(_to_utc(datetime.combine(day, _time_or_default(match["time"])), zone))

    match = _TODAY.match(lowered)
    if match:
        local = now.astimezone(zone)
        return OneShot(
            _to_utc(datetime.combine(local.date(), _time_or_default(match["time"])), zone)
        )

    match = _ISO_DATE.match(lowered)
    if match:
        try:
            day = date.fromisoformat(match["date"])
        except ValueError:
            # Shaped like a date but isn't one (2026-02-30). Reject it here
            # rather than letting a raw ValueError out of the parser.
            raise ScheduleError(f"`{match['date']}` isn't a date on the calendar.") from None
        return OneShot(_to_utc(datetime.combine(day, _time_or_default(match["time"])), zone))

    for pattern in (_MONTH_DAY, _DAY_MONTH):
        match = pattern.match(lowered)
        if match:
            return OneShot(_dated_one_shot(match, now=now, zone=zone))

    match = _NEXT_WEEKDAY.match(lowered)
    if match:
        weekday = WEEKDAY_NAMES[match["day"]]
        return OneShot(
            _next_local(_time_or_default(match["time"]), zone, now, weekday=weekday)
        )

    # Last, because it is the loosest: anything left that reads as a time of day
    # means the next time it comes round.
    try:
        when = parse_time_of_day(lowered)
    except ScheduleError:
        return None
    return OneShot(_next_local(when, zone, now))


def _dated_one_shot(match: re.Match, *, now: datetime, zone: ZoneInfo) -> datetime:
    """A named-month date, defaulting to the next year it hasn't happened in."""
    month = MONTH_NAMES[match["month"]]
    day = int(match["day"])
    when = _time_or_default(match["time"])
    local_now = now.astimezone(zone)
    year = int(match["year"]) if match["year"] else local_now.year
    for candidate in (year, year + 1) if not match["year"] else (year,):
        try:
            moment = _to_utc(datetime.combine(date(candidate, month, day), when), zone)
        except ValueError as exc:
            # 30 February and friends. Only reachable for a real date the
            # calendar doesn't have, so say which.
            raise ScheduleError(
                f"There's no {day} {match['month'].title()} in {candidate}."
            ) from exc
        if moment > now:
            return moment
    return moment


def _unreadable(raw: str) -> str:
    forms = "\n".join(f"• {form}" for form in ACCEPTED_FORMS)
    subject = f"`{raw}`" if raw else "An empty schedule"
    return f"{subject} isn't a schedule I can read. These are:\n{forms}"


# --------------------------------------------------------------------------
# Round-tripping a stored task
# --------------------------------------------------------------------------


def parse_stored(kind: str, spec: str, tz: str = "UTC") -> Schedule:
    """Rebuild the schedule a row was stored from.

    The inverse of ``schedule.kind`` + :attr:`Schedule.spec`. Strict about
    malformed input, which here means a corrupted row rather than a typo.
    """
    if kind == OneShot.kind:
        return OneShot(_parse_instant(spec))
    if kind == Interval.kind:
        anchor_text, _, seconds = spec.rpartition("/")
        if not anchor_text:
            raise ScheduleError(f"Malformed interval spec {spec!r}.")
        try:
            every = timedelta(seconds=int(seconds))
        except ValueError:
            raise ScheduleError(f"Malformed interval spec {spec!r}.") from None
        return Interval(every=every, anchor=_parse_instant(anchor_text))
    if kind == Cron.kind:
        return parse_cron(spec, tz=tz)
    raise ScheduleError(f"Unknown schedule kind {kind!r}.")


def _parse_instant(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ScheduleError(f"Malformed timestamp {text!r}.") from None
    # A stored instant is always UTC; a naive one means it lost its offset in
    # transit, and reading it as anything but UTC would move the task.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
