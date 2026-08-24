"""Tests for the rumor rules in :mod:`wojbot.core.rumor`.

The fixtures below are the real seed data from ``leagueSQL/sql/dml``, not
invented values -- the point of most of these tests is that the rules produce
sensible *league* outcomes ('a statement comes from the GM, not from a rival
team's players'), and that only holds against the real levels.
"""

from __future__ import annotations

import pytest

from wojbot.core.rumor import (
    COMMISSIONER_SOURCE_ID,
    MAX_RUMOR_LENGTH,
    ReleaseType,
    RumorForm,
    RumorTooLong,
    SourceType,
    check_fill_length,
    fill_fields,
    forms_for,
    is_reportable,
    releases_for_source,
    render_form,
    render_source,
    reportable_sources,
    sources_for,
)

# dim_release_type: (id, name, level, valid_forms)
RELEASES = [
    ReleaseType(1, "none", 0, (1, 2, 7)),
    ReleaseType(2, "rumor", 0, (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14)),
    ReleaseType(4, "denial", 1, (1, 5, 11)),
    ReleaseType(9, "low-level leak", 1, (1, 2, 3, 7, 8, 13)),
    ReleaseType(6, "discussion", 2, tuple(range(1, 15))),
    ReleaseType(7, "talks", 2, (1, 3, 4, 5, 6, 9, 10, 11, 12, 13)),
    ReleaseType(8, "high-level leak", 2, (1, 3, 4, 5, 6, 9, 10, 11, 12)),
    ReleaseType(5, "release", 3, (1,)),
    ReleaseType(3, "statement", 4, (1, 2, 3, 4, 5, 6, 9, 10, 11, 12)),
    ReleaseType(10, "league release", 5, (1,)),
]

# dim_source_type: (id, name, level, is_internal)
SOURCES = [
    SourceType(1, "the GM of {team}", 4, True),
    SourceType(2, "President of Basketball Ops, {manager}", 4, True),
    SourceType(3, "higher ups within the {team} front office", 3, True),
    SourceType(4, "members of the {team} front office", 2, True),
    SourceType(5, "{team} execs", 3, True),
    SourceType(6, "representatives of {team}", 2, True),
    SourceType(7, "rival team(s)", 1, False),
    SourceType(8, "the front office of {team}", 2, False),
    SourceType(9, "the GM of {team}", 2, False),
    SourceType(10, "the Commissioner", 5, True),
    SourceType(11, "the agent of {player}", 1, False),
    SourceType(12, "representatives of {player}", 1, False),
    SourceType(13, "players on {team}", 1, True),
    SourceType(14, "anonymous sources", 0, False),
]

FREE_FORM = RumorForm(1, '"{rumor}"', "Free Form", ("rumor",), ())
QUIET = RumorForm(
    2,
    "“Discussions have been quietly circulating in the {team} front office "
    "about {rumor}.”",
    "Quiet",
    ("rumor",),
    (3, 4, 5, 6),
)


def release(name: str) -> ReleaseType:
    return next(r for r in RELEASES if r.name == name)


def ids(sources: list[SourceType]) -> set[int]:
    return {s.id for s in sources}


# --- which sources may be reported at all ---------------------------------


def test_player_sources_are_not_reportable():
    """{player} comes from a form fill, and no enabled form offers one."""
    assert not is_reportable(SOURCES[10])  # the agent of {player}
    assert not is_reportable(SOURCES[11])  # representatives of {player}


def test_external_team_sources_are_not_reportable():
    """An outsider's {team} is somebody else's club, which nothing resolves yet."""
    assert not is_reportable(SOURCES[7])  # the front office of {team}, external
    assert not is_reportable(SOURCES[8])  # the GM of {team}, external


def test_external_sources_without_placeholders_are_reportable():
    """Nothing to resolve means nothing in the way."""
    assert is_reportable(SOURCES[6])   # rival team(s)
    assert is_reportable(SOURCES[13])  # anonymous sources


def test_internal_sources_are_reportable():
    assert all(is_reportable(s) for s in SOURCES if s.is_internal)


# --- the authority rule ---------------------------------------------------


def test_league_release_takes_only_the_commissioner():
    """Level 5 is the league's own voice; nobody else reaches it."""
    allowed = sources_for(release("league release"), SOURCES, privileged=True)
    assert ids(allowed) == {COMMISSIONER_SOURCE_ID}


def test_statement_takes_only_the_front_office_top():
    allowed = sources_for(release("statement"), SOURCES, privileged=True)
    assert ids(allowed) == {1, 2, COMMISSIONER_SOURCE_ID}


def test_release_reaches_the_exec_tier():
    allowed = sources_for(release("release"), SOURCES, privileged=True)
    assert ids(allowed) == {1, 2, 3, 5, COMMISSIONER_SOURCE_ID}


def test_high_level_leak_reaches_the_front_office():
    allowed = sources_for(release("high-level leak"), SOURCES, privileged=True)
    assert ids(allowed) == {1, 2, 3, 4, 5, 6, COMMISSIONER_SOURCE_ID}


def test_low_level_leak_reaches_players_and_rivals():
    allowed = sources_for(release("low-level leak"), SOURCES, privileged=True)
    assert ids(allowed) == {1, 2, 3, 4, 5, 6, 7, 13, COMMISSIONER_SOURCE_ID}


def test_only_a_bare_rumor_can_be_anonymous():
    """Level 0 is the one rung 'anonymous sources' reaches."""
    for name in ("none", "rumor"):
        assert 14 in ids(sources_for(release(name), SOURCES, privileged=True))
    for name in ("denial", "talks", "statement", "league release"):
        assert 14 not in ids(sources_for(release(name), SOURCES, privileged=True))


def test_a_source_never_carries_a_heavier_release_than_its_level():
    for rel in RELEASES:
        for source in sources_for(rel, SOURCES, privileged=True):
            assert source.level >= rel.level, (rel.name, source.name)


def test_sources_are_offered_most_senior_first():
    allowed = sources_for(release("rumor"), SOURCES, privileged=True)
    assert [s.level for s in allowed] == sorted((s.level for s in allowed), reverse=True)


# --- the commissioner gate ------------------------------------------------


def test_commissioner_is_hidden_without_privilege():
    allowed = sources_for(release("rumor"), SOURCES, privileged=False)
    assert COMMISSIONER_SOURCE_ID not in ids(allowed)


def test_commissioner_appears_with_privilege():
    allowed = sources_for(release("rumor"), SOURCES, privileged=True)
    assert COMMISSIONER_SOURCE_ID in ids(allowed)


def test_league_release_has_no_sources_without_privilege():
    """The one release only the Commissioner can carry becomes a dead end."""
    assert sources_for(release("league release"), SOURCES, privileged=False) == []


# --- forms ----------------------------------------------------------------


def test_empty_valid_sources_means_unrestricted():
    """Form 1 is seeded '{}'; reading that as 'nobody' would make it unusable."""
    assert FREE_FORM.valid_sources == ()
    for source in sources_for(release("statement"), SOURCES, privileged=True):
        assert forms_for(release("statement"), source, [FREE_FORM]) == [FREE_FORM]


def test_form_must_be_listed_by_the_release_type():
    """'release' permits only form 1, so a form 2 offer would be wrong."""
    gm = SOURCES[0]
    assert forms_for(release("release"), gm, [FREE_FORM, QUIET]) == [FREE_FORM]


def test_disabled_forms_are_never_offered():
    """Only ENABLED_FORM_IDS reach a reporter, whatever the dims allow."""
    execs = SOURCES[4]  # {team} execs, id 5 -- one of QUIET's valid_sources
    assert QUIET.id in release("discussion").valid_forms
    assert execs.id in QUIET.valid_sources
    # Everything the dims ask for lines up, and it is still not offered.
    assert forms_for(release("discussion"), execs, [QUIET]) == []


# --- picking the leaker first ---------------------------------------------


def test_every_reportable_source_is_offered_to_a_reporter():
    """Form 1 is open to everyone, so nothing reportable is a dead end."""
    offered = reportable_sources(RELEASES, SOURCES, [FREE_FORM], privileged=True)
    assert ids(offered) == {s.id for s in SOURCES if is_reportable(s)}


def test_unreportable_sources_are_never_offered():
    """The ones nothing can resolve stay off the first list too."""
    offered = reportable_sources(RELEASES, SOURCES, [FREE_FORM], privileged=True)
    assert ids(offered).isdisjoint({8, 9, 11, 12})


def test_commissioner_is_offered_as_a_leaker_only_with_privilege():
    assert COMMISSIONER_SOURCE_ID in ids(
        reportable_sources(RELEASES, SOURCES, [FREE_FORM], privileged=True)
    )
    assert COMMISSIONER_SOURCE_ID not in ids(
        reportable_sources(RELEASES, SOURCES, [FREE_FORM], privileged=False)
    )


def test_a_source_with_no_usable_form_is_not_offered():
    """QUIET is disabled, so the sources it lists have nothing behind it."""
    assert reportable_sources(RELEASES, SOURCES, [QUIET], privileged=True) == []


def test_leakers_are_offered_most_senior_first():
    offered = reportable_sources(RELEASES, SOURCES, [FREE_FORM], privileged=True)
    assert [s.level for s in offered] == sorted(
        (s.level for s in offered), reverse=True
    )


def test_the_commissioner_can_carry_everything():
    commissioner = next(s for s in SOURCES if s.id == COMMISSIONER_SOURCE_ID)
    offered = releases_for_source(commissioner, RELEASES, [FREE_FORM])
    assert {r.id for r in offered} == {r.id for r in RELEASES}
    assert [r.level for r in offered] == sorted(r.level for r in RELEASES)


def test_anonymous_sources_carry_only_the_quietest_releases():
    """Level 0 hears nothing louder than itself."""
    anonymous = SOURCES[13]
    offered = releases_for_source(anonymous, RELEASES, [FREE_FORM])
    assert {r.name for r in offered} == {"none", "rumor"}


def test_a_release_is_never_offered_above_its_leaker():
    for source in reportable_sources(RELEASES, SOURCES, [FREE_FORM], privileged=True):
        for rel in releases_for_source(source, RELEASES, [FREE_FORM]):
            assert source.level >= rel.level, (source.name, rel.name)


def test_the_two_directions_agree():
    """Whatever a source is offered, that release would have offered it back."""
    for privileged in (False, True):
        offered = reportable_sources(
            RELEASES, SOURCES, [FREE_FORM], privileged=privileged
        )
        for source in offered:
            releases = releases_for_source(source, RELEASES, [FREE_FORM])
            assert releases, source.name
            for rel in releases:
                assert source.id in ids(
                    sources_for(rel, SOURCES, privileged=privileged)
                ), (source.name, rel.name)


def test_every_offered_leaker_has_at_least_one_release():
    for privileged in (False, True):
        for source in reportable_sources(
            RELEASES, SOURCES, [FREE_FORM], privileged=privileged
        ):
            assert releases_for_source(source, RELEASES, [FREE_FORM]), source.name


# --- rendering ------------------------------------------------------------


def test_render_source_fills_team_and_manager():
    assert render_source(
        SOURCES[0], team_name="Waiting for Jamot", manager_name="GeneralH"
    ) == "the GM of Waiting for Jamot"
    assert render_source(
        SOURCES[1], team_name="Waiting for Jamot", manager_name="GeneralH"
    ) == "President of Basketball Ops, GeneralH"


def test_render_source_leaves_literal_punctuation_alone():
    """'rival team(s)' would blow up str.format; str.replace doesn't care."""
    assert render_source(SOURCES[6], team_name="X", manager_name="Y") == "rival team(s)"


def test_rendering_survives_unknown_braces():
    text = "{team} said {something_else}"
    assert fill_fields(text, team_name="X", manager_name="Y") == "X said {something_else}"


def test_render_form_fills_the_body():
    body = render_form(FREE_FORM, {"rumor": "We're not trading Jokic."})
    assert body == '"We\'re not trading Jokic."'


def test_rendering_does_not_interpret_reporter_text():
    """Whatever a reporter types is content, never a format directive."""
    body = render_form(FREE_FORM, {"rumor": "{team} {manager} {rumor} {0} %s"})
    assert body == '"{team} {manager} {rumor} {0} %s"'


def test_a_rumor_at_the_limit_is_accepted():
    text = "x" * MAX_RUMOR_LENGTH
    assert check_fill_length(text) == text


def test_a_rumor_over_the_limit_is_rejected():
    with pytest.raises(RumorTooLong):
        check_fill_length("x" * (MAX_RUMOR_LENGTH + 1))


def test_the_length_error_names_both_numbers():
    """The reporter has to know how much to cut."""
    with pytest.raises(RumorTooLong, match=rf"{MAX_RUMOR_LENGTH}.*{MAX_RUMOR_LENGTH + 50}"):
        check_fill_length("x" * (MAX_RUMOR_LENGTH + 50))


def test_over_length_text_is_rejected_not_truncated():
    """Truncating would put words in the reporter's mouth without telling them."""
    with pytest.raises(RumorTooLong):
        check_fill_length("We are trading " + "x" * MAX_RUMOR_LENGTH)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
