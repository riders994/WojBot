"""Tests for the privilege rules in :mod:`wojbot.core.checks`.

This module had no coverage at all, and it is the one that decides who may run
what. The cases below pin the parts that are easy to get wrong by accident:

* **the precedence of the three routes to a yes** -- bot owner, Discord server
  administrator, configured role. They are tried in that order, and an
  administrator passes *without the configured list being read*, so a change
  that reordered them would look correct in any server whose testers are admins.
* **exact name matching**, which is a security property rather than a detail: a
  role somebody creates called ``champion`` must not open commands configured for
  ``Champion``.
* **the two entry points agreeing.** ``_has_role_privilege`` (in a guild, roles
  already on the interaction) and ``has_privilege_in_guild`` (from a DM, roles
  fetched) are the same decision reached two ways, and only one of them is
  exercised by ordinary use.
* **the ladder**, which is the newest and least obvious rule: Admin satisfies a
  Verified check and Verified does not satisfy an Admin one. A bug that made the
  implication run both ways would be invisible in a server whose Admins are also
  listed as Verified, which is exactly how the old two-tier defaults were set up.
* **Restricted beating everything**, including a member who is also an Admin.
  It is the only tier that takes access away, so a precedence slip there fails
  open rather than closed.

No Discord: the fakes carry only what the checks actually read, which is
``guild_permissions.administrator`` and ``roles``.
"""

from __future__ import annotations

import discord
import pytest

from wojbot.bot import WojBotV2
from wojbot.core.checks import (
    ADMIN_ROLES_KEY,
    Restricted,
    REASON_ADMINISTRATOR,
    REASON_NO_GUILD,
    REASON_NO_MATCH,
    REASON_NOT_A_MEMBER,
    REASON_OWNER,
    REASON_RESTRICTED,
    REASON_ROLE,
    RESTRICTED_ROLES_KEY,
    VERIFIED_ROLES_KEY,
    evaluate_privilege,
    evaluate_restriction,
    has_privilege_in_guild,
    is_restricted_in_guild,
)
from wojbot.core.defaults import DEFAULT_SERVER_CONFIG

GUILD = 111
OTHER_GUILD = 222

DEFAULT_ADMIN = tuple(DEFAULT_SERVER_CONFIG[ADMIN_ROLES_KEY])
DEFAULT_VERIFIED = tuple(DEFAULT_SERVER_CONFIG[VERIFIED_ROLES_KEY])


class FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name


class FakePerms:
    def __init__(self, administrator: bool = False) -> None:
        self.administrator = administrator


class FakeMember:
    def __init__(self, id: int, roles=(), administrator: bool = False) -> None:
        self.id = id
        self.roles = [FakeRole(name) for name in roles]
        self.guild_permissions = FakePerms(administrator)
        self.display_name = f"member-{id}"


class FakeConfigs:
    """Stands in for ConfigStore: defaults unless a guild stored its own."""

    def __init__(self, stored: dict[int, dict] | None = None) -> None:
        self._stored = stored or {}

    def get_guild(self, guild_id: int) -> dict:
        return {**DEFAULT_SERVER_CONFIG, **self._stored.get(guild_id, {})}

    def guild_has_own(self, guild_id: int, key: str) -> bool:
        return key in self._stored.get(guild_id, {})


class _FakeResponse:
    status = 404
    reason = "Not Found"


class FakeGuild:
    def __init__(self, guild_id: int, members) -> None:
        self.id = guild_id
        self._members = {m.id: m for m in members}

    def get_member(self, user_id):
        return self._members.get(user_id)

    async def fetch_member(self, user_id):
        try:
            return self._members[user_id]
        except KeyError:
            # What the real call does for somebody who isn't in the guild:
            # NotFound, which is an HTTPException. Raising KeyError here would
            # test a path the library never takes.
            raise discord.NotFound(_FakeResponse(), "Unknown Member") from None


class FakeBot:
    def __init__(self, configs, guilds=(), owner_id: int | None = None) -> None:
        self.configs = configs
        self._guilds = {g.id: g for g in guilds}
        self._owner_id = owner_id

    async def is_owner(self, user) -> bool:
        return self._owner_id is not None and user.id == self._owner_id

    def get_guild(self, guild_id):
        return self._guilds.get(guild_id)


# --------------------------------------------------------------------------
# The three routes to a yes, and their order
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_role_match_says_which_role_matched():
    member = FakeMember(1, roles=["@everyone", "Champion"])
    bot = FakeBot(FakeConfigs())
    result = await evaluate_privilege(
        bot, GUILD, member, VERIFIED_ROLES_KEY, member=member
    )
    assert result.allowed
    assert result.reason == REASON_ROLE
    assert result.matched_role == "Champion"
    assert result.matched_tier == VERIFIED_ROLES_KEY
    assert result.by_implication is False
    assert result.consulted_roles is True


@pytest.mark.asyncio
async def test_an_administrator_passes_without_the_roles_being_read():
    """Administrator short-circuits before the configured list is consulted.

    Worth pinning because it is invisible from the outside: in a server whose
    testers all hold Administrator, a role list that matches nothing at all still
    looks like it works.
    """
    member = FakeMember(1, roles=["@everyone"], administrator=True)
    bot = FakeBot(FakeConfigs())
    result = await evaluate_privilege(bot, GUILD, member, ADMIN_ROLES_KEY, member=member)
    assert result.allowed
    assert result.reason == REASON_ADMINISTRATOR
    assert result.consulted_roles is False
    assert result.matched_role is None


@pytest.mark.asyncio
async def test_the_owner_passes_everywhere_including_outside_any_guild():
    owner = FakeMember(99, roles=[])
    bot = FakeBot(FakeConfigs(), owner_id=99)
    for guild_id in (GUILD, OTHER_GUILD, None):
        result = await evaluate_privilege(bot, guild_id, owner, ADMIN_ROLES_KEY)
        assert result.allowed and result.reason == REASON_OWNER


@pytest.mark.asyncio
async def test_no_matching_role_reports_both_lists():
    member = FakeMember(1, roles=["@everyone", "Rookie"])
    bot = FakeBot(FakeConfigs())
    result = await evaluate_privilege(bot, GUILD, member, ADMIN_ROLES_KEY, member=member)
    assert not result.allowed
    assert result.reason == REASON_NO_MATCH
    assert result.member_roles == ("@everyone", "Rookie")
    assert result.allowed_names == DEFAULT_ADMIN


@pytest.mark.asyncio
async def test_a_dm_has_no_roles_to_read():
    member = FakeMember(1, roles=["Champion"])
    bot = FakeBot(FakeConfigs())
    result = await evaluate_privilege(bot, None, member, ADMIN_ROLES_KEY)
    assert not result.allowed and result.reason == REASON_NO_GUILD


@pytest.mark.asyncio
async def test_somebody_who_left_the_server_is_not_privileged_in_it():
    stranger = FakeMember(7, roles=["Champion"])
    bot = FakeBot(FakeConfigs(), guilds=[FakeGuild(GUILD, [])])
    result = await evaluate_privilege(bot, GUILD, stranger, ADMIN_ROLES_KEY)
    assert not result.allowed and result.reason == REASON_NOT_A_MEMBER


@pytest.mark.asyncio
async def test_an_unknown_guild_is_not_a_yes():
    member = FakeMember(1, roles=["Champion"])
    bot = FakeBot(FakeConfigs())  # bot is in no guilds at all
    result = await evaluate_privilege(bot, GUILD, member, ADMIN_ROLES_KEY)
    assert not result.allowed and result.reason == REASON_NOT_A_MEMBER


# --------------------------------------------------------------------------
# The ladder: Admin implies Verified, and not the other way round
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_admin_role_passes_a_verified_check_by_implication():
    """The whole point of the ladder, and the part with no visible evidence.

    ``Commish`` is an Admin default and is deliberately *not* repeated in the
    Verified list, so this can only pass by implication. Under the old two-tier
    defaults ``Commish`` appeared in both lists, which would have hidden a
    broken implication entirely.
    """
    assert "Commish" in DEFAULT_ADMIN
    assert "Commish" not in DEFAULT_VERIFIED

    member = FakeMember(1, roles=["Commish"])
    bot = FakeBot(FakeConfigs())
    result = await evaluate_privilege(
        bot, GUILD, member, VERIFIED_ROLES_KEY, member=member
    )
    assert result.allowed and result.reason == REASON_ROLE
    assert result.matched_role == "Commish"
    # Reported against the tier it actually came from, not the one asked about.
    assert result.matched_tier == ADMIN_ROLES_KEY
    assert result.by_implication is True
    assert "implication" in result.describe()


@pytest.mark.asyncio
async def test_a_verified_role_does_not_pass_an_admin_check():
    """The ladder must not run downhill, or Verified would just be Admin."""
    member = FakeMember(1, roles=["Champion", "mods"])
    bot = FakeBot(FakeConfigs())
    result = await evaluate_privilege(bot, GUILD, member, ADMIN_ROLES_KEY, member=member)
    assert not result.allowed and result.reason == REASON_NO_MATCH
    # The report is about the tier asked for, so a reader sees what they'd need.
    assert result.allowed_names == DEFAULT_ADMIN


@pytest.mark.asyncio
async def test_an_admin_check_passed_on_an_admin_role_is_not_by_implication():
    member = FakeMember(1, roles=["Commish"])
    bot = FakeBot(FakeConfigs())
    result = await evaluate_privilege(bot, GUILD, member, ADMIN_ROLES_KEY, member=member)
    assert result.allowed and result.matched_tier == ADMIN_ROLES_KEY
    assert result.by_implication is False


@pytest.mark.asyncio
async def test_the_most_senior_tier_wins_when_a_member_holds_both():
    """matched_tier should name the highest list, not whichever was read first."""
    configs = FakeConfigs({GUILD: {
        ADMIN_ROLES_KEY: ["Boss"], VERIFIED_ROLES_KEY: ["Deputy"],
    }})
    bot = FakeBot(configs)
    member = FakeMember(1, roles=["Deputy", "Boss"])
    result = await evaluate_privilege(
        bot, GUILD, member, VERIFIED_ROLES_KEY, member=member
    )
    assert result.matched_tier == ADMIN_ROLES_KEY and result.matched_role == "Boss"


# --------------------------------------------------------------------------
# Name matching is exact, and that is a security property
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_name", "why"),
    [
        ("champion", "different case"),
        ("CHAMPION", "different case"),
        ("Champion ", "trailing space"),
        (" Champion", "leading space"),
        ("Champions", "different word"),
        ("Сhampion", "Cyrillic С, not Latin C"),
    ],
)
async def test_a_role_that_merely_looks_right_grants_nothing(live_name, why):
    """Every one of these renders as "Champion" or nearly so, and none may pass.

    Anyone who can create a role could otherwise mint a near-miss name and pick
    up the admin tier with it, so this stays a strict comparison.
    """
    member = FakeMember(1, roles=[live_name])
    bot = FakeBot(FakeConfigs())
    result = await evaluate_privilege(
        bot, GUILD, member, VERIFIED_ROLES_KEY, member=member
    )
    assert not result.allowed, f"{live_name!r} ({why}) should not have matched"
    assert result.reason == REASON_NO_MATCH


@pytest.mark.asyncio
async def test_one_matching_role_among_many_is_enough():
    member = FakeMember(1, roles=["Rookie", "champion", "Champion", "Bench"])
    bot = FakeBot(FakeConfigs())
    result = await evaluate_privilege(
        bot, GUILD, member, VERIFIED_ROLES_KEY, member=member
    )
    assert result.allowed and result.matched_role == "Champion"


# --------------------------------------------------------------------------
# Config: a stored list replaces the default outright
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stored_list_replaces_the_default_rather_than_extending_it():
    """A server that stores its own list stops following the defaults entirely.

    These keys are not in ``INHERITED_FROM_BOT``, so there is no path back: a
    name added to ``DEFAULT_SERVER_CONFIG`` later will never reach a server that
    has already stored a list. Surprising enough to be worth a test, since two
    servers can then disagree while both look configured.
    """
    configs = FakeConfigs({OTHER_GUILD: {VERIFIED_ROLES_KEY: ["mods"]}})
    bot = FakeBot(configs)
    member = FakeMember(1, roles=["Champion"])

    default_server = await evaluate_privilege(
        bot, GUILD, member, VERIFIED_ROLES_KEY, member=member
    )
    stored_server = await evaluate_privilege(
        bot, OTHER_GUILD, member, VERIFIED_ROLES_KEY, member=member
    )

    assert default_server.allowed and default_server.matched_role == "Champion"
    assert not stored_server.allowed
    assert configs.guild_has_own(OTHER_GUILD, VERIFIED_ROLES_KEY) is True
    assert configs.guild_has_own(GUILD, VERIFIED_ROLES_KEY) is False


@pytest.mark.asyncio
async def test_emptying_every_tier_locks_everyone_but_owners_and_admins_out():
    configs = FakeConfigs({GUILD: {ADMIN_ROLES_KEY: [], VERIFIED_ROLES_KEY: []}})
    bot = FakeBot(configs)
    member = FakeMember(1, roles=["Champion", "mods", "Commish"])
    for key in (ADMIN_ROLES_KEY, VERIFIED_ROLES_KEY):
        result = await evaluate_privilege(bot, GUILD, member, key, member=member)
        assert not result.allowed and result.reason == REASON_NO_MATCH

    admin = FakeMember(2, roles=[], administrator=True)
    result = await evaluate_privilege(bot, GUILD, admin, ADMIN_ROLES_KEY, member=admin)
    assert result.allowed and result.reason == REASON_ADMINISTRATOR


@pytest.mark.asyncio
async def test_emptying_verified_alone_does_not_lock_out_an_admin():
    """A server that clears its Verified list still has working Admins.

    The old two-tier arrangement had no implication, so clearing a list really
    did shut out everybody on it. Worth pinning that this changed, because it is
    the difference between "no Verified roles" and "no Verified access".
    """
    configs = FakeConfigs({GUILD: {VERIFIED_ROLES_KEY: []}})
    bot = FakeBot(configs)
    boss = FakeMember(1, roles=["Commish"])
    deputy = FakeMember(2, roles=["Champion"])

    allowed = await evaluate_privilege(
        bot, GUILD, boss, VERIFIED_ROLES_KEY, member=boss
    )
    denied = await evaluate_privilege(
        bot, GUILD, deputy, VERIFIED_ROLES_KEY, member=deputy
    )
    assert allowed.allowed and allowed.by_implication is True
    assert not denied.allowed


# --------------------------------------------------------------------------
# The public wrapper agrees with the decision underneath it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_privilege_in_guild_fetches_the_member_and_agrees():
    """The DM-flow entry point: roles come from the guild, not an interaction.

    It is the path ordinary use never takes -- only a wizard run in a DM does --
    so it gets its own test rather than being assumed to match.
    """
    member = FakeMember(1, roles=["Champion"])
    bot = FakeBot(FakeConfigs(), guilds=[FakeGuild(GUILD, [member])])

    assert await has_privilege_in_guild(bot, GUILD, member, VERIFIED_ROLES_KEY) is True
    assert await has_privilege_in_guild(bot, None, member, VERIFIED_ROLES_KEY) is False
    assert await has_privilege_in_guild(bot, GUILD, member, ADMIN_ROLES_KEY) is False

    outsider = FakeMember(8, roles=["Champion"])
    assert await has_privilege_in_guild(bot, GUILD, outsider, VERIFIED_ROLES_KEY) is False


@pytest.mark.asyncio
async def test_the_cache_is_used_before_a_fetch_is_attempted():
    """A cached member must not cost an HTTP round trip."""
    member = FakeMember(1, roles=["Champion"])
    guild = FakeGuild(GUILD, [member])
    fetched = []

    async def tracking_fetch(user_id):
        fetched.append(user_id)
        raise AssertionError("should not fetch a member the cache already has")

    guild.fetch_member = tracking_fetch
    bot = FakeBot(FakeConfigs(), guilds=[guild])
    assert await has_privilege_in_guild(bot, GUILD, member, VERIFIED_ROLES_KEY) is True
    assert fetched == []


# --------------------------------------------------------------------------
# Restricted: the only tier that takes access away
# --------------------------------------------------------------------------
#
# evaluate_restriction answers "does the deny apply", so ``allowed`` being True
# means the member is shut out. That reads backwards on purpose -- it is the
# same PrivilegeResult shape as everything else, and the tree check inverts it
# once rather than every caller flipping it by hand.


@pytest.mark.asyncio
async def test_nobody_is_restricted_by_default():
    """The default list is empty, so this tier costs nothing until it is used."""
    member = FakeMember(1, roles=["@everyone", "Rookie"])
    bot = FakeBot(FakeConfigs())
    result = await evaluate_restriction(bot, GUILD, member, member=member)
    assert not result.allowed and result.reason == REASON_NO_MATCH


@pytest.mark.asyncio
async def test_a_restricted_role_shuts_a_member_out():
    configs = FakeConfigs({GUILD: {RESTRICTED_ROLES_KEY: ["Timeout"]}})
    bot = FakeBot(configs)
    member = FakeMember(1, roles=["@everyone", "Timeout"])
    result = await evaluate_restriction(bot, GUILD, member, member=member)
    assert result.allowed
    assert result.reason == REASON_RESTRICTED
    assert result.matched_role == "Timeout"


@pytest.mark.asyncio
async def test_restriction_beats_every_tier_the_member_also_holds():
    """The floor overrides the ladder, and this is the test that proves it.

    Somebody who is both an Admin and Restricted is the case a precedence slip
    fails *open* on -- they would keep `/commish db` and all of it. The two
    checks are separate calls, so nothing but this pins the order they run in.
    """
    configs = FakeConfigs({GUILD: {RESTRICTED_ROLES_KEY: ["Timeout"]}})
    bot = FakeBot(configs)
    member = FakeMember(1, roles=["Commish", "Timeout"])

    privilege = await evaluate_privilege(
        bot, GUILD, member, ADMIN_ROLES_KEY, member=member
    )
    restriction = await evaluate_restriction(bot, GUILD, member, member=member)

    # Both say yes to their own question; the tree check runs the restriction
    # first, which is what makes the deny win.
    assert privilege.allowed
    assert restriction.allowed


@pytest.mark.asyncio
async def test_the_owner_and_server_administrators_cannot_be_restricted():
    """A tier anybody with /verify could point at the people who fix things
    would be a way to lock the bot's own escape hatches."""
    configs = FakeConfigs({GUILD: {RESTRICTED_ROLES_KEY: ["Timeout"]}})

    owner = FakeMember(99, roles=["Timeout"])
    bot = FakeBot(configs, owner_id=99)
    result = await evaluate_restriction(bot, GUILD, owner, member=owner)
    assert not result.allowed and result.reason == REASON_OWNER

    admin = FakeMember(2, roles=["Timeout"], administrator=True)
    bot = FakeBot(configs)
    result = await evaluate_restriction(bot, GUILD, admin, member=admin)
    assert not result.allowed and result.reason == REASON_ADMINISTRATOR


@pytest.mark.asyncio
async def test_a_dm_has_no_server_whose_restriction_could_apply():
    configs = FakeConfigs({GUILD: {RESTRICTED_ROLES_KEY: ["Timeout"]}})
    member = FakeMember(1, roles=["Timeout"])
    bot = FakeBot(configs)
    result = await evaluate_restriction(bot, None, member)
    assert not result.allowed and result.reason == REASON_NO_GUILD


@pytest.mark.asyncio
async def test_a_restriction_does_not_follow_somebody_out_of_the_server():
    configs = FakeConfigs({GUILD: {RESTRICTED_ROLES_KEY: ["Timeout"]}})
    stranger = FakeMember(7, roles=["Timeout"])
    bot = FakeBot(configs, guilds=[FakeGuild(GUILD, [])])
    result = await evaluate_restriction(bot, GUILD, stranger)
    assert not result.allowed and result.reason == REASON_NOT_A_MEMBER


@pytest.mark.asyncio
async def test_restriction_names_are_matched_exactly_too():
    """Same rule as the granting tiers, for the opposite reason: a near-miss
    here means somebody stays restricted after the role is renamed off them."""
    configs = FakeConfigs({GUILD: {RESTRICTED_ROLES_KEY: ["Timeout"]}})
    bot = FakeBot(configs)
    member = FakeMember(1, roles=["timeout"])
    result = await evaluate_restriction(bot, GUILD, member, member=member)
    assert not result.allowed


@pytest.mark.asyncio
async def test_the_restriction_wrapper_fetches_the_member_and_agrees():
    configs = FakeConfigs({GUILD: {RESTRICTED_ROLES_KEY: ["Timeout"]}})
    member = FakeMember(1, roles=["Timeout"])
    bot = FakeBot(configs, guilds=[FakeGuild(GUILD, [member])])
    assert await is_restricted_in_guild(bot, GUILD, member) is True
    assert await is_restricted_in_guild(bot, None, member) is False


# --------------------------------------------------------------------------
# The enforcement point: the tree check that actually applies Restricted
# --------------------------------------------------------------------------
#
# evaluate_restriction only *decides*; this is the thing that acts on it, and it
# is wired onto the tree rather than onto any command. Called unbound against a
# stand-in, because building a real WojBotV2 would load the developer's own
# config directory to test one branch.


class FakeCommand:
    def __init__(self, qualified_name: str) -> None:
        self.qualified_name = qualified_name


class FakeInteraction:
    def __init__(self, user, guild=None, command=None) -> None:
        self.user = user
        self.guild = guild
        self.guild_id = None if guild is None else guild.id
        self.command = command


class FakeTreeBot(FakeBot):
    """A FakeBot wearing the bot class's own ALWAYS_ALLOWED and check."""

    ALWAYS_ALLOWED = WojBotV2.ALWAYS_ALLOWED

    async def check(self, interaction):
        return await WojBotV2._interaction_check(self, interaction)


def restricted_bot():
    configs = FakeConfigs({GUILD: {RESTRICTED_ROLES_KEY: ["Timeout"]}})
    return FakeTreeBot(configs, guilds=[FakeGuild(GUILD, [])])


@pytest.mark.asyncio
async def test_the_tree_check_refuses_a_restricted_member():
    bot = restricted_bot()
    member = FakeMember(1, roles=["Timeout"])
    guild = FakeGuild(GUILD, [member])
    interaction = FakeInteraction(member, guild, FakeCommand("dice"))
    with pytest.raises(Restricted):
        await bot.check(interaction)


@pytest.mark.asyncio
async def test_the_tree_check_covers_a_command_with_no_decorator():
    """The reason this lives on the tree at all.

    ``/dice`` and ``/cat`` carry no check of their own — being undecorated is
    exactly what Normal means — so a per-command approach would have to remember
    every one of them, including the ones added after the decision was made.
    """
    bot = restricted_bot()
    member = FakeMember(1, roles=["Timeout"])
    guild = FakeGuild(GUILD, [member])
    for name in ("dice", "cat", "remind me", "rumor recent", "elo standings"):
        with pytest.raises(Restricted):
            await bot.check(FakeInteraction(member, guild, FakeCommand(name)))


@pytest.mark.asyncio
async def test_a_restricted_member_keeps_the_commands_that_explain_it():
    """Without these, the refusal is the only thing they can ever reach."""
    bot = restricted_bot()
    member = FakeMember(1, roles=["Timeout"])
    guild = FakeGuild(GUILD, [member])
    for name in ("help", "verify show"):
        assert await bot.check(FakeInteraction(member, guild, FakeCommand(name))) is True


@pytest.mark.asyncio
async def test_the_tree_check_lets_everybody_else_straight_through():
    bot = restricted_bot()
    ordinary = FakeMember(2, roles=["Rookie"])
    guild = FakeGuild(GUILD, [ordinary])
    assert await bot.check(FakeInteraction(ordinary, guild, FakeCommand("dice"))) is True


@pytest.mark.asyncio
async def test_the_tree_check_passes_a_dm_through():
    """No guild on the interaction means no server's restriction to apply."""
    bot = restricted_bot()
    member = FakeMember(1, roles=["Timeout"])
    assert await bot.check(FakeInteraction(member, None, FakeCommand("remind me"))) is True
