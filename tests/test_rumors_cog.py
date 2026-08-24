"""Tests for the restriction rule in :mod:`wojbot.cogs.rumors`.

``/rumor`` is the one command group that isn't ``guild_only``, and that is what
makes it the one place the global tree check can't reach: in a DM there is no
server on the interaction, so the Restricted tier has nothing to read. Left
there, being shut out of a league's server would stop somebody reporting in it
and not stop them reporting *to* it from a DM -- the same wire, the same
rumor, reached by opening a different window.

So the rule is asked again once the league is known, of the league's own server.
The cases below pin the part with a decision in it: which server gets asked, and
that a league bound to none falls back to the one the command came from rather
than to nobody. The Discord plumbing around it -- selects, embeds, modals -- is
left alone, in keeping with the rest of the suite.
"""

from __future__ import annotations

import pytest

from wojbot.cogs.rumors import league_guild_id, refuse_if_restricted
from wojbot.core.checks import RESTRICTED_ROLES_KEY
from wojbot.core.rumor import League

# The same fakes the checks tests use. Imported rather than copied: they stand
# in for what the privilege code actually reads, and a second copy would be free
# to drift from the first.
from .test_checks import FakeBot, FakeConfigs, FakeGuild, FakeMember

LEAGUE_GUILD = 111
OTHER_GUILD = 222

BOUND = League(1, "Macho Mandarins", LEAGUE_GUILD)
UNBOUND = League(2, "Orphan League", None)


def restricted_bot(*, guilds, restricted_in=(LEAGUE_GUILD,)):
    configs = FakeConfigs({
        guild_id: {RESTRICTED_ROLES_KEY: ["Timeout"]} for guild_id in restricted_in
    })
    return FakeBot(configs, guilds=guilds)


# --- which server is asked -------------------------------------------------


def test_a_bound_league_is_judged_by_its_own_server():
    """Not by wherever the command was typed. That is the whole rule."""
    assert league_guild_id(BOUND, OTHER_GUILD) == LEAGUE_GUILD


def test_a_dm_has_no_invoking_server_to_fall_back_to():
    assert league_guild_id(BOUND, None) == LEAGUE_GUILD


def test_an_unbound_league_falls_back_to_the_invoking_server():
    """A league the bot never bound has no server of its own to ask."""
    assert league_guild_id(UNBOUND, OTHER_GUILD) == OTHER_GUILD


def test_an_unbound_league_in_a_dm_has_nobody_to_ask():
    assert league_guild_id(UNBOUND, None) is None


# --- the refusal -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restricted_reporter_is_refused_from_a_dm():
    """The hole this closes: no guild on the interaction, restricted anyway."""
    member = FakeMember(1, roles=["Timeout"])
    bot = restricted_bot(guilds=[FakeGuild(LEAGUE_GUILD, [member])])

    refusal = await refuse_if_restricted(bot, BOUND, member, None)

    assert refusal is not None
    # Named, because in a DM nothing else on screen says which server this was.
    assert "Macho Mandarins" in refusal


@pytest.mark.asyncio
async def test_an_unrestricted_reporter_proceeds():
    member = FakeMember(1, roles=["Champion"])
    bot = restricted_bot(guilds=[FakeGuild(LEAGUE_GUILD, [member])])

    assert await refuse_if_restricted(bot, BOUND, member, None) is None


@pytest.mark.asyncio
async def test_being_restricted_somewhere_else_doesnt_reach_this_league():
    """Restriction is per-server, so it travels with the league and no further.

    A member restricted in the server they happen to be typing in is still
    reporting into a league whose own server has nothing against them.
    """
    member = FakeMember(1, roles=["Timeout"])
    bot = restricted_bot(
        guilds=[FakeGuild(LEAGUE_GUILD, [member]), FakeGuild(OTHER_GUILD, [member])],
        restricted_in=(OTHER_GUILD,),
    )

    assert await refuse_if_restricted(bot, BOUND, member, OTHER_GUILD) is None


@pytest.mark.asyncio
async def test_the_league_server_decides_even_when_typed_somewhere_permissive():
    """The mirror of the case above, and the one somebody would try."""
    member = FakeMember(1, roles=["Timeout"])
    bot = restricted_bot(
        guilds=[FakeGuild(LEAGUE_GUILD, [member]), FakeGuild(OTHER_GUILD, [member])],
    )

    assert await refuse_if_restricted(bot, BOUND, member, OTHER_GUILD) is not None


@pytest.mark.asyncio
async def test_the_action_is_named_so_reading_and_reporting_read_differently():
    member = FakeMember(1, roles=["Timeout"])
    bot = restricted_bot(guilds=[FakeGuild(LEAGUE_GUILD, [member])])

    reading = await refuse_if_restricted(
        bot, BOUND, member, None, action="read its rumors"
    )

    assert "read its rumors" in reading


# --- the exemptions match the rest of the ladder ---------------------------


@pytest.mark.asyncio
async def test_a_server_administrator_is_never_restricted():
    """Same exemption the tier itself grants; the DM path must not tighten it."""
    member = FakeMember(1, roles=["Timeout"], administrator=True)
    bot = restricted_bot(guilds=[FakeGuild(LEAGUE_GUILD, [member])])

    assert await refuse_if_restricted(bot, BOUND, member, None) is None


@pytest.mark.asyncio
async def test_somebody_who_left_the_league_server_is_not_restricted_by_it():
    """No membership, no roles to hold against them.

    They will fail to resolve a reporter a moment later for the same reason;
    what matters here is that they are not refused as *restricted*, which would
    be a different and wrong explanation.
    """
    member = FakeMember(1, roles=["Timeout"])
    bot = restricted_bot(guilds=[FakeGuild(LEAGUE_GUILD, [])])

    assert await refuse_if_restricted(bot, BOUND, member, None) is None
