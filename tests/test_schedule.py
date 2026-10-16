"""Tests for the scheduling rules in :mod:`wojbot.core.schedule`.

Every test fixes ``now`` explicitly. The module takes the current time as an
argument precisely so that these can, and a test that read the real clock would
pass or fail depending on what day of the week it ran.

``NOW`` is a Monday, chosen so "monday" tests both "later today" and "next week"
depending on the time of day, and the timezone is one with daylight saving so the
DST tests have something to cross.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wojbot.core.schedule import (
    MAX_HORIZON,
    Cron,
    Interval,
    OneShot,
    ScheduleError,
    available_zones,
    parse_cron,
    parse_duration,
    parse_schedule,
    parse_stored,
    parse_time_of_day,
    resolve_zone,
)

UTC = timezone.utc
TZ = "America/New_York"
# Monday 17 August 2026, 11:30 EDT.
NOW = datetime(2026, 8, 17, 15, 30, tzinfo=UTC)


def when(text: str, *, now: datetime = NOW, tz: str = TZ) -> datetime:
    """The next firing of ``text``, as a UTC instant."""
    schedule = parse_schedule(text, now=now, tz=tz)
    fire = schedule.next_fire_after(now)
    assert fire is not None
    return fire


def local(text: str, *, now: datetime = NOW, tz: str = TZ) -> str:
    """The next firing of ``text``, as a local wall-clock string."""
    return f"{when(text, now=now, tz=tz).astimezone(resolve_zone(tz)):%a %Y-%m-%d %H:%M %Z}"


# --------------------------------------------------------------------------
# Durations and times of day
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90m", timedelta(minutes=90)),
        ("2h30m", timedelta(hours=2, minutes=30)),
        ("3 days", timedelta(days=3)),
        ("1 hour and 30 minutes", timedelta(hours=1, minutes=30)),
        ("2w", timedelta(weeks=2)),
        ("45 mins", timedelta(minutes=45)),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "5", "banana", "0m", "3 fortnights", "90m banana"])
def test_parse_duration_rejects(text):
    """A duration that can't be read fully is refused, never guessed at."""
    with pytest.raises(ScheduleError):
        parse_duration(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9am", (9, 0)), ("9pm", (21, 0)), ("12am", (0, 0)), ("12pm", (12, 0)),
        ("9:30am", (9, 30)), ("21:30", (21, 30)), ("09:05", (9, 5)),
        ("noon", (12, 0)), ("midnight", (0, 0)), ("9.30pm", (21, 30)),
    ],
)
def test_parse_time_of_day(text, expected):
    parsed = parse_time_of_day(text)
    assert (parsed.hour, parsed.minute) == expected


@pytest.mark.parametrize("text", ["9", "25:00", "13pm", "9:60", "0am", "half nine"])
def test_parse_time_of_day_rejects(text):
    """A bare hour is refused: `9` is as likely a day of the month as a time."""
    with pytest.raises(ScheduleError):
        parse_time_of_day(text)


# --------------------------------------------------------------------------
# One-shots
# --------------------------------------------------------------------------


def test_relative_offsets_ignore_the_timezone():
    """"in 90m" is 90 minutes wherever you are; the zone never enters into it."""
    assert when("in 90m") == NOW + timedelta(minutes=90)
    assert when("in 90m", tz="Asia/Tokyo") == NOW + timedelta(minutes=90)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # NOW is 11:30 local, so 9am has gone and 9pm has not.
        ("9am", "Tue 2026-08-18 09:00 EDT"),
        ("9pm", "Mon 2026-08-17 21:00 EDT"),
        ("noon", "Mon 2026-08-17 12:00 EDT"),
        ("tomorrow 8am", "Tue 2026-08-18 08:00 EDT"),
        ("today 11pm", "Mon 2026-08-17 23:00 EDT"),
        ("friday 8pm", "Fri 2026-08-21 20:00 EDT"),
        # Today is Monday and 9am has passed, so "monday" means next week.
        ("monday 9am", "Mon 2026-08-24 09:00 EDT"),
        # ...but a later time today still means today.
        ("monday 3pm", "Mon 2026-08-17 15:00 EDT"),
        ("sep 1 at 9am", "Tue 2026-09-01 09:00 EDT"),
        ("1st of september", "Tue 2026-09-01 09:00 EDT"),
        ("2026-09-01 14:00", "Tue 2026-09-01 14:00 EDT"),
    ],
)
def test_one_shot_phrases(text, expected):
    assert local(text) == expected


def test_a_date_with_no_time_defaults_to_nine():
    """A day named without a time means the morning, not midnight."""
    assert local("friday") == "Fri 2026-08-21 09:00 EDT"


def test_a_past_month_day_rolls_to_next_year():
    """"jan 5" in August means next January, not one eight months gone."""
    assert local("jan 5 at 9am") == "Tue 2027-01-05 09:00 EST"


def test_an_explicit_past_year_is_refused():
    """A year somebody typed is taken at their word, and then refused as past."""
    with pytest.raises(ScheduleError, match="past"):
        parse_schedule("jan 5 2020 at 9am", now=NOW, tz=TZ)


def test_a_one_shot_is_exhausted_once_it_has_fired():
    schedule = parse_schedule("in 90m", now=NOW, tz=TZ)
    fire = schedule.next_fire_after(NOW)
    assert schedule.next_fire_after(fire) is None
    assert schedule.recurring is False


def test_the_horizon_is_a_ceiling():
    far = NOW + MAX_HORIZON + timedelta(days=30)
    with pytest.raises(ScheduleError, match="limit"):
        parse_schedule(f"{far:%Y-%m-%d} 09:00", now=NOW, tz=TZ)


@pytest.mark.parametrize("text", ["2026-02-30 9am", "feb 30 at 9am", "2026-13-01"])
def test_dates_that_arent_on_the_calendar(text):
    with pytest.raises(ScheduleError):
        parse_schedule(text, now=NOW, tz=TZ)


# --------------------------------------------------------------------------
# Recurrence
# --------------------------------------------------------------------------


def test_interval_first_firing_is_one_period_out():
    """"every 3d" starts in three days, not now."""
    schedule = parse_schedule("every 3d", now=NOW, tz=TZ)
    assert isinstance(schedule, Interval)
    assert schedule.next_fire_after(NOW) == NOW + timedelta(days=3)


def test_interval_does_not_drift_when_a_firing_is_late():
    """A delivery that ran late must not push the ones after it.

    This is why the anchor is stored rather than the last firing: adding the
    period to "when it actually went out" walks a daily reminder around the clock
    one outage at a time.
    """
    anchor = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    schedule = Interval(every=timedelta(hours=1), anchor=anchor)
    # The bot was down for two and a half hours.
    late = datetime(2026, 8, 17, 14, 37, tzinfo=UTC)
    assert schedule.next_fire_after(late) == datetime(2026, 8, 17, 15, 0, tzinfo=UTC)


def test_interval_refuses_to_be_tighter_than_a_minute():
    with pytest.raises(ScheduleError, match="shortest"):
        parse_schedule("every 10s", now=NOW, tz=TZ)


@pytest.mark.parametrize(
    ("text", "spec"),
    [
        ("daily at 9am", "0 9 * * *"),
        ("daily at 6:30pm", "30 18 * * *"),
        ("daily", "0 9 * * *"),  # no time given: DEFAULT_TIME
        ("weekly on monday at 9am", "0 9 * * 1"),
        ("weekly monday at 9am", "0 9 * * 1"),  # "on" is optional
        ("weekly on mondays and fridays at 7pm", "0 19 * * 1,5"),
        ("weekdays at 8:30am", "30 8 * * 1,2,3,4,5"),
        ("weekends at 10am", "0 10 * * 0,6"),
        ("monthly on the 1st at 9am", "0 9 1 * *"),
        ("monthly on the 15th at noon", "0 12 15 * *"),
        ("hourly", "0 * * * *"),
        ("cron 0 9 * * MON", "0 9 * * 1"),
        ("cron */15 * * * *", "0,15,30,45 * * * *"),
    ],
)
def test_calendar_phrases_become_cron(text, spec):
    schedule = parse_schedule(text, now=NOW, tz=TZ)
    assert isinstance(schedule, Cron)
    assert schedule.spec == spec
    assert schedule.recurring is True


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("every 3d", 259200), ("every 12h", 43200), ("every 90m", 5400),
     ("every 1 hour and 30 minutes", 5400), ("every 2w", 1209600)],
)
def test_every_takes_a_number_and_builds_an_interval(text, seconds):
    """`every` is the interval keyword, and an interval is elapsed time."""
    schedule = parse_schedule(text, now=NOW, tz=TZ)
    assert isinstance(schedule, Interval)
    assert schedule.every.total_seconds() == seconds


@pytest.mark.parametrize(
    ("text", "suggestion"),
    [
        ("every monday at 9am", "weekly on monday at 9am"),
        ("every mondays", "weekly on monday"),
        ("every day at 9am", "daily at 9am"),
        ("every weekday at 8am", "weekdays at 8am"),
        ("every weekend", "weekends"),
        ("every hour", "hourly"),
        ("every month on the 15th", "monthly on the 15th"),
        ("every week", "weekly on <day>"),
    ],
)
def test_every_with_a_calendar_word_hands_back_the_right_phrase(text, suggestion):
    """The point of making `every` numeric-only: the error has to fix it.

    Splitting the two kinds of repeat only reduces confusion if somebody who
    types the old phrasing is told the new one, rather than being handed a list
    to work it out from.
    """
    with pytest.raises(ScheduleError) as caught:
        parse_schedule(text, now=NOW, tz=TZ)
    assert f"`{suggestion}`" in str(caught.value)


@pytest.mark.parametrize("text", ["weekly", "weekly on", "weekly at 9am"])
def test_weekly_on_its_own_asks_which_day(text):
    """daily/monthly/hourly know when they mean; weekly can't be guessed at."""
    with pytest.raises(ScheduleError, match="needs a day"):
        parse_schedule(text, now=NOW, tz=TZ)


@pytest.mark.parametrize(
    ("text", "spec"), [("daily", "0 9 * * *"), ("monthly", "0 9 1 * *"), ("hourly", "0 * * * *")]
)
def test_the_other_calendar_words_stand_alone(text, spec):
    assert parse_schedule(text, now=NOW, tz=TZ).spec == spec


def test_every_with_nothing_recognisable_lists_the_calendar_forms():
    with pytest.raises(ScheduleError) as caught:
        parse_schedule("every other tuesday", now=NOW, tz=TZ)
    message = str(caught.value)
    assert "length of time" in message
    assert "weekly on monday at 9am" in message


def test_cron_weekday_seven_is_sunday():
    """Both 0 and 7 mean Sunday in a crontab, and they must land on the same set."""
    assert parse_cron("0 9 * * 0").weekday == parse_cron("0 9 * * 7").weekday


def test_cron_ors_day_of_month_with_day_of_week():
    """crontab's own rule: `0 9 13 * FRI` is the 13th *and* every Friday."""
    schedule = parse_cron("0 9 13 * FRI", tz="UTC")
    fires = []
    moment = datetime(2026, 11, 1, tzinfo=UTC)
    for _ in range(4):
        moment = schedule.next_fire_after(moment)
        fires.append(f"{moment:%a %d %b}")
    # Fridays in November 2026 are the 6th, 13th, 20th, 27th; the 13th is both.
    assert fires == ["Fri 06 Nov", "Fri 13 Nov", "Fri 20 Nov", "Fri 27 Nov"]


def test_cron_with_no_future_match_is_exhausted():
    """30 February never comes round, and saying so beats searching forever."""
    never = Cron(
        minute=frozenset({0}), hour=frozenset({0}), day=frozenset({30}),
        month=frozenset({2}), weekday=None, tz="UTC",
    )
    assert never.next_fire_after(NOW) is None


@pytest.mark.parametrize(
    "expression",
    ["0 9 * *", "0 9 * * * *", "60 9 * * *", "0 24 * * *", "0 9 32 * *",
     "0 9 * 13 *", "0 9 * * 8", "x 9 * * *", "0 9 5-1 * *", "*/0 * * * *"],
)
def test_bad_cron_expressions(expression):
    with pytest.raises(ScheduleError):
        parse_cron(expression)


# --------------------------------------------------------------------------
# Timezones and daylight saving
# --------------------------------------------------------------------------


def test_a_wall_clock_time_is_read_in_the_given_zone():
    """The same phrase is a different instant in a different zone."""
    assert when("today 11pm", tz="America/New_York") != when("today 11pm", tz="Asia/Tokyo")


def test_an_explicit_zone_overrides_the_servers():
    tokyo = when("2026-09-01 09:00", tz="Asia/Tokyo")
    assert f"{tokyo:%H:%M}" == "00:00"  # 9am JST is midnight UTC


def test_cron_holds_its_wall_clock_time_across_a_dst_change():
    """The whole reason a Cron stores a timezone instead of a UTC offset.

    US daylight saving ends on Sunday 1 November 2026. A 9am task must be 9am on
    both sides of it -- which means the UTC instant has to move.
    """
    schedule = parse_schedule("daily at 9am", now=NOW, tz=TZ)
    zone = resolve_zone(TZ)
    moment = datetime(2026, 10, 30, 13, 0, tzinfo=UTC)
    seen = []
    for _ in range(4):
        moment = schedule.next_fire_after(moment)
        seen.append(f"{moment.astimezone(zone):%a %d %b %H:%M %Z}")
    assert seen == [
        "Sat 31 Oct 09:00 EDT",
        "Sun 01 Nov 09:00 EST",  # clocks went back; still 9am
        "Mon 02 Nov 09:00 EST",
        "Tue 03 Nov 09:00 EST",
    ]


def test_interval_holds_elapsed_time_across_a_dst_change():
    """The counterpart: "every 24h" is 24 elapsed hours, so the wall clock moves.

    Not a bug -- it is the difference between the two kinds, and the reason both
    exist.
    """
    schedule = Interval(
        every=timedelta(hours=24), anchor=datetime(2026, 10, 31, 13, 0, tzinfo=UTC)
    )
    zone = resolve_zone(TZ)
    first = schedule.next_fire_after(datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    second = schedule.next_fire_after(first)
    assert f"{first.astimezone(zone):%H:%M %Z}" == "09:00 EDT"
    assert f"{second.astimezone(zone):%H:%M %Z}" == "08:00 EST"


def test_an_ambiguous_local_time_fires_once():
    """1:30am happens twice when the clocks go back. It must not fire twice."""
    schedule = Cron(
        minute=frozenset({30}), hour=frozenset({1}), day=None, month=None,
        weekday=None, tz=TZ,
    )
    first = schedule.next_fire_after(datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    second = schedule.next_fire_after(first)
    # The next firing is the following day, not the repeated hour.
    assert second - first > timedelta(hours=23)


def test_a_nonexistent_local_time_still_fires():
    """2:30am doesn't exist when the clocks go forward, but the task still runs.

    Resolved with the pre-transition offset, which lands it at 3:30am local that
    day. Skipping the day entirely would be the worse answer.
    """
    schedule = Cron(
        minute=frozenset({30}), hour=frozenset({2}), day=None, month=None,
        weekday=None, tz=TZ,
    )
    fire = schedule.next_fire_after(datetime(2026, 3, 7, 12, 0, tzinfo=UTC))
    assert f"{fire.astimezone(resolve_zone(TZ)):%d %b %H:%M}" == "08 Mar 03:30"


def test_unknown_zones_are_refused():
    with pytest.raises(ScheduleError, match="timezone"):
        resolve_zone("Mars/Olympus_Mons")
    with pytest.raises(ScheduleError, match="timezone"):
        parse_schedule("9am", now=NOW, tz="Not/AZone")


def test_available_zones_is_real_and_sorted():
    zones = available_zones()
    assert "UTC" in zones and TZ in zones
    assert zones == sorted(zones)


# --------------------------------------------------------------------------
# Storage round-trip
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["in 90m", "friday 8pm", "every 3d", "weekly on monday at 9am",
     "cron */15 9-17 * * *", "monthly on the 1st at 9am", "2026-09-01 14:00",
     "hourly", "weekdays at 8:30am"],
)
def test_every_schedule_round_trips_through_storage(text):
    """What goes in a row has to come back as the same schedule.

    The three stored columns are kind, spec and timezone; if these three cannot
    rebuild the object, a restart silently changes when things fire.
    """
    original = parse_schedule(text, now=NOW, tz=TZ)
    restored = parse_stored(original.kind, original.spec, TZ)
    assert restored == original
    assert restored.next_fire_after(NOW) == original.next_fire_after(NOW)


def test_a_stored_instant_with_no_offset_is_read_as_utc():
    """Naive means it lost its offset in transit; anything but UTC moves the task."""
    assert parse_stored("once", "2026-09-01T14:00:00") == OneShot(
        datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    ("kind", "spec"),
    [("once", "not-a-date"), ("interval", "900"), ("interval", "2026-09-01T00:00:00/x"),
     ("cron", "0 9 * *"), ("nonsense", "whatever")],
)
def test_malformed_stored_schedules_are_refused(kind, spec):
    with pytest.raises(ScheduleError):
        parse_stored(kind, spec)


# --------------------------------------------------------------------------
# Unreadable input
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "", "   ", "banana", "when the season starts", "9pm-ish",
        # A calendar word with no keyword in front of it at all.
        "on tuesdays", "twice a week",
    ],
)
def test_unreadable_schedules_list_the_ones_that_work(text):
    """The error is the parser's documentation, so it has to carry the forms."""
    with pytest.raises(ScheduleError) as caught:
        parse_schedule(text, now=NOW, tz=TZ)
    assert "in 90m" in str(caught.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("in a bit", "length of time"),
        ("cron 0 9 * *", "five fields"),
        # Parsed as a duration and then refused for being too short -- a
        # different failure from "every" not taking a duration at all.
        ("every 10s", "shortest repeat"),
        ("jan 5 2020 at 9am", "past"),
    ],
)
def test_a_phrase_that_half_parses_says_which_half(text, expected):
    """Once a phrase has committed to a form, the error is about that form.

    "every so often" is recognisably a repeat, so being told what a repeat needs
    beats being handed the whole list again -- the generic list is for input that
    matched nothing at all.
    """
    with pytest.raises(ScheduleError, match=expected):
        parse_schedule(text, now=NOW, tz=TZ)


def test_naive_now_is_a_programming_error():
    with pytest.raises(ScheduleError, match="aware"):
        parse_schedule("in 5m", now=datetime(2026, 8, 17, 15, 30), tz=TZ)
