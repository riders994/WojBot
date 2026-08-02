"""The rumor domain: what may be reported, by whom, and how it reads.

A rumor is three choices plus some text. A **release type** is how loudly it is
being said ("rumor", "statement", "league release"); a **source type** is who is
saying it ("the GM of {team}", "anonymous sources"); a **rumor form** is the
sentence it gets poured into. Both the source name and the form text are format
strings, filled from whoever reported it.

Two rules decide which combinations are legal, and only one of them lives in the
database:

* ``dim_release_type.valid_forms`` lists the forms a release type may use, and
  ``dim_rumor_form.valid_sources`` the sources a form may carry (empty meaning
  unrestricted, not forbidden -- form 1 is seeded ``'{}'``).
* **Authority**: ``source_type_level >= release_type_level``. A release type
  carries weight, and only a source with at least that much standing can put it
  out -- a "statement" comes from the GM or the President of Basketball Ops, a
  "league release" only from the Commissioner, while a mere "rumor" (level 0)
  will take anybody, down to anonymous sources. Nothing in the schema encodes
  this; it is here. (The abandoned v1 in ``archive/`` compares these the other
  way round. It was never finished and its reading makes 'rumor' accept *only*
  anonymous sources -- don't port it.)

The first half of this module is pure -- no Discord, no database -- so the rules
can be tested directly. The second half reads and writes them through
:class:`wojbot.core.sql.SqlService`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from .config import PROJECT_ROOT
from .discord_anon import CATEGORY_MANAGER, CATEGORY_SERVER

log = logging.getLogger(__name__)

# Guild setting naming the channel reported rumors are posted to. Lives here
# rather than in the setup cog because both that cog and the rumor cog read it.
RUMOR_CHANNEL_KEY = "rumor_channel"

# The only form wired up so far: '"{rumor}"', a single free-form fill. The rest
# of dim_rumor_form needs multi-fill support and structured storage, so the
# wizard offers this one and this constant is the switch that widens it.
ENABLED_FORM_IDS: tuple[int, ...] = (1,)

# 'the Commissioner' -- the league's own voice, so it is not a source any
# manager may claim. Gated on the admin_roles tier in the league's server.
COMMISSIONER_SOURCE_ID = 10

# How much a reporter may type into one fill. Discord's own ceiling on a
# paragraph TextInput is 4000, and ``rumor_text`` is an unbounded ``varchar``,
# so this number is the only thing that bounds the column -- it lives here
# rather than in the cog because the modal is a courtesy and this is the rule.
MAX_RUMOR_LENGTH = 1000

# Placeholders a source name can carry, and what fills them.
TEAM_FIELD = "{team}"
MANAGER_FIELD = "{manager}"
PLAYER_FIELD = "{player}"


# --------------------------------------------------------------------------
# Domain
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseType:
    """A row of ``dim_release_type``: how loudly the rumor is being said."""

    id: int
    name: str
    level: int
    valid_forms: tuple[int, ...] = ()


@dataclass(frozen=True)
class SourceType:
    """A row of ``dim_source_type``: who is saying it.

    ``name`` is a format string -- see :func:`render_source`.
    """

    id: int
    name: str
    level: int
    is_internal: bool

    @property
    def placeholders(self) -> tuple[str, ...]:
        """Which ``{...}`` fields this source's name needs filled."""
        return tuple(
            field for field in (TEAM_FIELD, MANAGER_FIELD, PLAYER_FIELD)
            if field in self.name
        )


@dataclass(frozen=True)
class RumorForm:
    """A row of ``dim_rumor_form``: the sentence the rumor is poured into."""

    id: int
    text: str
    title: str
    required_fills: tuple[str, ...] = ()
    valid_sources: tuple[int, ...] = ()


class RumorTooLong(ValueError):
    """A fill longer than :data:`MAX_RUMOR_LENGTH`. The message is for the reporter."""


def check_fill_length(text: str, *, limit: int = MAX_RUMOR_LENGTH) -> str:
    """Return ``text`` if it fits, else raise :class:`RumorTooLong`.

    Rejects rather than truncates: a rumor cut off mid-sentence reads as the
    reporter's own words, and they can't tell it happened.
    """
    if len(text) > limit:
        raise RumorTooLong(
            f"A rumor can be at most {limit} characters — that one is {len(text)}."
        )
    return text


def is_reportable(source: SourceType) -> bool:
    """Whether the bot can fill this source's name without extra input.

    An internal source speaks for the reporter's own club, so ``{team}`` and
    ``{manager}`` resolve from the reporter. An external one does not: "the GM
    of {team}" from outside means somebody *else's* GM, which needs a team the
    reporter picks. Those are excluded until the wizard grows that step, along
    with anything wanting ``{player}`` -- that comes from a form fill, and the
    only form wired up has none.

    An external source with no placeholder at all ('rival team(s)', 'anonymous
    sources') is fine: there is nothing to resolve.
    """
    if PLAYER_FIELD in source.name:
        return False
    return source.is_internal or not source.placeholders


def sources_for(
    release: ReleaseType,
    sources: list[SourceType],
    *,
    privileged: bool = False,
) -> list[SourceType]:
    """The sources allowed to put out ``release``, most authoritative first.

    Args:
        release: the chosen release type.
        sources: every known source type.
        privileged: whether the reporter passes the admin tier in the league's
            server, which is what unlocks the Commissioner.
    """
    allowed = [
        source for source in sources
        if source.level >= release.level
        and is_reportable(source)
        and (privileged or source.id != COMMISSIONER_SOURCE_ID)
    ]
    return sorted(allowed, key=lambda s: (-s.level, s.id))


def releases_for(
    releases: list[ReleaseType],
    sources: list[SourceType],
    forms: list[RumorForm],
    *,
    privileged: bool = False,
) -> list[ReleaseType]:
    """The release types this reporter could actually see through.

    Filtered by who is asking, not just by what the dims allow. A release type
    with no enabled form, or no source this reporter may speak as, is a dead
    end -- 'league release' takes only the Commissioner, and offering it to
    everyone else just means picking it and finding nothing behind it.
    """
    enabled = {form.id for form in forms if form.id in ENABLED_FORM_IDS}
    allowed = [
        release for release in releases
        if enabled.intersection(release.valid_forms)
        and sources_for(release, sources, privileged=privileged)
    ]
    return sorted(allowed, key=lambda r: (r.level, r.name))


def forms_for(
    release: ReleaseType,
    source: SourceType,
    forms: list[RumorForm],
) -> list[RumorForm]:
    """The forms this release/source pair may be written in.

    An empty ``valid_sources`` means the form takes any source. Reading it as
    "no source qualifies" would leave form 1 -- the free form, seeded ``'{}'``
    -- with nothing that could ever use it.
    """
    return [
        form for form in forms
        if form.id in ENABLED_FORM_IDS
        and form.id in release.valid_forms
        and (not form.valid_sources or source.id in form.valid_sources)
    ]


def fill_fields(text: str, *, team_name: str, manager_name: str) -> str:
    """Fill ``{team}`` and ``{manager}`` in a league-authored format string.

    Substitution is by :meth:`str.replace`, not :meth:`str.format`: these
    strings hold literal punctuation ('rival team(s)') and any brace the bot
    does not recognise should survive rather than raise.
    """
    return text.replace(TEAM_FIELD, team_name).replace(MANAGER_FIELD, manager_name)


def fill_form(text: str, fills: dict[str, str]) -> str:
    """Fill a form's placeholders from ``fills`` (same substitution rules)."""
    for field, value in fills.items():
        text = text.replace("{" + field + "}", value)
    return text


def render_source(source: SourceType, *, team_name: str, manager_name: str) -> str:
    """Fill a source type's name."""
    return fill_fields(source.name, team_name=team_name, manager_name=manager_name)


def render_form(form: RumorForm, fills: dict[str, str]) -> str:
    """Fill a rumor form's text."""
    return fill_form(form.text, fills)


# --------------------------------------------------------------------------
# Identity resolution and storage
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class League:
    """A league the reporter belongs to, and the server it is bound to."""

    id: int
    name: str
    guild_id: int | None  # real Discord id, or None if never bound by the bot


@dataclass(frozen=True)
class Reporter:
    """Who is reporting, resolved for one league's current season.

    ``team_name`` and ``display_name`` are what fill ``{team}`` and
    ``{manager}``; both are season-specific, which is why the season is carried
    with them and stored on the rumor.
    """

    manager_id: int
    league: League
    online_league_id: int
    league_year: int
    team_name: str
    display_name: str


@dataclass(frozen=True)
class Rumor:
    """One reported rumor, joined up and ready to print."""

    id: int
    reported_at: datetime
    release_name: str
    release_level: int
    source_text: str  # already rendered
    body: str  # already rendered

    @property
    def timestamp(self) -> str:
        """The report time as a Discord relative timestamp."""
        return f"<t:{int(self.reported_at.timestamp())}:R>"


class RumorDims:
    """The three lookup dimensions, read once and kept.

    They change only when somebody edits the seed data, so the cog loads them
    on first use rather than on every command.
    """

    def __init__(
        self,
        releases: list[ReleaseType],
        sources: list[SourceType],
        forms: list[RumorForm],
    ) -> None:
        self.releases = releases
        self.sources = sources
        self.forms = forms

    def release(self, release_id: int) -> ReleaseType | None:
        return next((r for r in self.releases if r.id == release_id), None)

    def source(self, source_id: int) -> SourceType | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def form(self, form_id: int) -> RumorForm | None:
        return next((f for f in self.forms if f.id == form_id), None)


async def load_dims(sql) -> RumorDims:
    """Read ``dim_release_type``, ``dim_source_type`` and ``dim_rumor_form``."""
    releases = await sql.run("read_dim_release_type")
    sources = await sql.run("read_dim_source_type")
    forms = await sql.run("read_dim_rumor_form")
    return RumorDims(
        releases=[
            ReleaseType(id=rid, name=name, level=level or 0,
                        valid_forms=tuple(valid or ()))
            for rid, name, level, valid in releases
        ],
        sources=[
            SourceType(id=sid, name=name, level=level or 0, is_internal=bool(internal))
            for sid, name, level, internal in sources
        ],
        forms=[
            RumorForm(id=fid, text=text, title=title,
                      required_fills=tuple(fills or ()),
                      valid_sources=tuple(valid or ()))
            for fid, text, title, fills, valid in forms
        ],
    )


async def resolve_manager(bot, user) -> int | None:
    """The ``manager_id`` behind a Discord user, or None if never linked.

    Looks up the surrogate rather than the real id -- real Discord ids are kept
    out of the warehouse entirely (:mod:`wojbot.core.discord_anon`). Linking is
    what ``/commish db linkuser`` does.
    """
    surrogate = bot.discord_anon.surrogate(CATEGORY_MANAGER, user.id)
    rows = await bot.sql.run("read_manager_by_discord", {"manager_surrogate": str(surrogate)})
    return rows[0][0] if rows else None


def _league(bot, league_id: int, name: str, stored_server_id) -> League:
    """Build a :class:`League`, turning the stored surrogate back into a guild id."""
    return League(
        id=league_id,
        name=name,
        guild_id=bot.discord_anon.real(CATEGORY_SERVER, stored_server_id),
    )


async def league_for_guild(bot, guild_id: int) -> League | None:
    """The league bound to this Discord server, or None if none is."""
    surrogate = bot.discord_anon.surrogate(CATEGORY_SERVER, guild_id)
    rows = await bot.sql.run("read_league_by_server", {"server_surrogate": surrogate})
    if not rows:
        return None
    league_id, name, stored = rows[0]
    return _league(bot, league_id, name, stored)


async def leagues_for_manager(bot, manager_id: int) -> list[League]:
    """Every league this manager plays in *this season*.

    Leagues they used to be in are left out: there is nothing to report in one,
    and offering it means asking somebody to choose between the league they play
    in and one they left.
    """
    rows = await bot.sql.run("read_manager_leagues", {"manager_id": manager_id})
    return [_league(bot, league_id, name, stored) for league_id, name, stored in rows]


async def current_season(bot, league: League) -> int | None:
    """The league's most recent season year, or None if it has no seasons."""
    rows = await bot.sql.run("read_league_current_season", {"league_id": league.id})
    return rows[0][1] if rows else None


async def resolve_reporter(bot, manager_id: int, league: League) -> Reporter | None:
    """Fill in a manager's team and display name for a league's current season.

    None means they have no team in that league this season -- nothing to report
    as. A season they *used* to have a team in doesn't count: the rumor would be
    stamped with a season that is over, spoken for a club somebody else runs now.
    """
    rows = await bot.sql.run(
        "read_manager_season", {"manager_id": manager_id, "league_id": league.id}
    )
    if not rows:
        return None
    online_league_id, league_year, _platform, team_name, stored_display = rows[0]
    return Reporter(
        manager_id=manager_id,
        league=league,
        online_league_id=online_league_id,
        league_year=league_year,
        team_name=team_name or league.name,
        display_name=display_name(stored_display),
    )


async def record_rumor(
    bot,
    reporter: Reporter,
    *,
    release: ReleaseType,
    source: SourceType,
    form: RumorForm,
    fills: dict[str, str],
) -> tuple[int, datetime]:
    """Write the rumor and return its ``(id, reported_at)``.

    Only the reporter's fill text is stored, not the rendered sentence: the
    form and source ids are stored too, so the line is rebuilt on the way out
    and a fixed typo in a form's wording reaches every rumor already written in
    it.

    The text is bound by psycopg2 (:meth:`SqlService.run`, never ``execute``),
    so it can only ever reach the database as a value; the length is checked
    here because this is the last point before the write, and the modal's
    ``max_length`` is a client-side courtesy rather than a guarantee.

    Raises:
        RumorTooLong: if a fill exceeds :data:`MAX_RUMOR_LENGTH`.
    """
    for value in fills.values():
        check_fill_length(value)
    rows = await bot.sql.run(
        "write_fact_rumor",
        {
            "league_id": reporter.league.id,
            "online_league_id": reporter.online_league_id,
            "league_year": reporter.league_year,
            "manager_id": reporter.manager_id,
            "rumor_form_id": form.id,
            "source_type_id": source.id,
            "release_type_id": release.id,
            # A single free-form fill today; multi-fill forms will need this
            # column to become structured.
            "rumor_text": fills[form.required_fills[0]] if form.required_fills else "",
        },
    )
    return rows[0][0], rows[0][1]


async def recent_rumors(bot, league: League, league_year: int, limit: int) -> list[Rumor]:
    """A league's most recent rumors this season, already rendered."""
    rows = await bot.sql.run(
        "read_fact_rumor_recent",
        {"league_id": league.id, "league_year": league_year, "limit": limit},
    )
    rumors = []
    for (rumor_id, reported_at, release_name, release_level, source_name,
         form_text, required_fills, rumor_text, team_name, stored_display) in rows:
        team = team_name or league.name
        manager = display_name(stored_display)
        # Single-fill forms only, matching what record_rumor stored: the one
        # column of text goes back into the one placeholder the form declares.
        fills = {required_fills[0]: rumor_text or ""} if required_fills else {}
        rumors.append(Rumor(
            id=rumor_id,
            reported_at=reported_at,
            release_name=release_name,
            release_level=release_level or 0,
            source_text=fill_fields(source_name, team_name=team, manager_name=manager),
            body=fill_fields(
                fill_form(form_text, fills), team_name=team, manager_name=manager,
            ),
        ))
    return rumors


# --------------------------------------------------------------------------
# Display names
# --------------------------------------------------------------------------

# dim_manager_platform.display_name is stored tokenised -- the elo package runs
# every name through rv_pytools' anonymizer on the way into the warehouse, so a
# raw read hands back 'manager_18' rather than the person's name. EloSQL
# reverses this when it pulls a dim into pandas; these queries don't go through
# it, so the reversal happens here. The file and label match ANON_COLS in
# elo_system.tools.basics.constants.
_ANON_MAP_PATH = PROJECT_ROOT / "resources" / "anon" / "anon_manager_platform.json"
_ANON_LABEL = "manager"

# Cached against the file's mtime rather than loaded once: /commish sync mints
# tokens for managers who joined since, and the bot outlives those syncs. A
# stale cache would print 'manager_42' at somebody for as long as it ran.
_name_map: dict[str, str] = {}
_name_map_mtime: float | None = None


def display_name(token: str | None) -> str:
    """The real display name behind an anonymizer token.

    Falls back to the token itself when there is no map entry -- a name the bot
    can't reverse still reads better in a rumor than an empty string.
    """
    global _name_map, _name_map_mtime
    if not token:
        return ""
    try:
        mtime = _ANON_MAP_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if mtime != _name_map_mtime:
        _name_map_mtime = mtime
        _name_map = {}
        if mtime is not None:
            try:
                with open(_ANON_MAP_PATH, encoding="utf-8") as handle:
                    _name_map = (json.load(handle) or {}).get(_ANON_LABEL, {})
            except (OSError, ValueError):
                log.warning("Could not read the display-name map at %s", _ANON_MAP_PATH)
    return _name_map.get(token, token)
