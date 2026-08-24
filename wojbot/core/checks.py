"""Reusable permission checks for slash commands.

Four tiers, and they are a **ladder** — each one passes everything below it::

    Admin       admin_roles       runs the league
    Verified    verified_roles    trusted to deputise
    Normal      (no roles)        the default; everybody else
    Restricted  restricted_roles  denied outright

Other cogs gate commands by decorating them::

    from ..core.checks import is_admin

    @app_commands.command()
    @is_admin()
    async def dump(self, interaction): ...

**Normal has no decorator, and cannot have one.** It is the absence of a check,
which is why it needed no representation here until Restricted existed. Nothing
in this module asks "are you Normal"; the question is only ever "are you at
least Verified" or "are you Restricted".

A user passes a positive check if they are the **bot owner**, a **Discord server
administrator**, or hold one of the role names configured for that tier *or any
tier above it* in the guild's settings (see
:data:`wojbot.core.defaults.DEFAULT_SERVER_CONFIG`, editable via ``/verify``).
Role matching is by name, matching v1 and keeping the YAML config
human-readable — renaming a role in Discord means updating it here too.

**Those are three independent routes to the same yes, and which one fired is not
recoverable from the boolean.** That is what :class:`PrivilegeResult` and
:func:`evaluate_privilege` are for: every decision comes back with its reason
attached, so "why does this role work in one server and not another" is a
question the bot can answer rather than one somebody has to guess at. The two
answers that look identical from outside and are not:

* passing as a server administrator, where the configured roles were never
  consulted at all, and
* passing on a role, where they were.

A server whose admins hold Administrator will see every role list "work" until
somebody without it tries.

Because the tiers imply downward, a third case joins them: passing a Verified
command *on an Admin role*. ``matched_tier`` records which list actually
matched, so a server that wonders why removing a role from ``verified_roles``
changed nothing can be told that the holder is an Admin.

Names are matched **exactly** — same case, same bytes. A role called ``champion``
does not open a command configured for ``Champion``, deliberately: folding names
to decide access would mean anyone able to create a role could open admin
commands with a near-miss spelling.

Restricted runs the other way and is checked somewhere else entirely: it is a
*denial* that has to beat the default-allow, so no per-command decorator can
carry it — a command with no decorator is exactly what Normal means. It is
enforced once, globally, by the tree check in :mod:`wojbot.bot`, which is why a
command added tomorrow is covered without anybody remembering to cover it. See
:func:`evaluate_restriction`.

A failed check raises :class:`discord.app_commands.CheckFailure` (or
:class:`Restricted`, a subclass), which the bot's tree error handler turns into
a friendly ephemeral reply.

The decorators are all guild-bound -- they read roles off the interaction, which
has none in a DM. :func:`has_privilege_in_guild` is the way to apply a server's
roles from outside it, for a flow that runs in a DM but acts on one league.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import app_commands

ADMIN_ROLES_KEY = "admin_roles"
VERIFIED_ROLES_KEY = "verified_roles"
RESTRICTED_ROLES_KEY = "restricted_roles"

# The positive tiers, **highest first**. Order is the ladder: a check for one
# tier is satisfied by that tier or anything ahead of it in this tuple. Adding a
# tier means putting it in the right place here and nowhere else.
TIER_LADDER: tuple[str, ...] = (ADMIN_ROLES_KEY, VERIFIED_ROLES_KEY)

# Display names, for anything that shows a tier to a human.
TIER_LABELS = {
    ADMIN_ROLES_KEY: "Admin",
    VERIFIED_ROLES_KEY: "Verified",
    RESTRICTED_ROLES_KEY: "Restricted",
}

# Why a privilege decision came out the way it did.
REASON_OWNER = "owner"
REASON_ADMINISTRATOR = "administrator"
REASON_ROLE = "role"
REASON_NO_MATCH = "no_match"
REASON_NOT_A_MEMBER = "not_a_member"
REASON_NO_GUILD = "no_guild"
REASON_RESTRICTED = "restricted"

_REASON_TEXT = {
    REASON_OWNER: "bot owner — passes everywhere, whatever the roles say",
    REASON_ADMINISTRATOR: (
        "Discord server administrator — passes without the configured roles "
        "being consulted at all"
    ),
    REASON_ROLE: "holds a configured role",
    REASON_NO_MATCH: "holds none of the configured roles",
    REASON_NOT_A_MEMBER: "not a member of that server",
    REASON_NO_GUILD: "no server to read roles from (a DM)",
    REASON_RESTRICTED: "holds a restricted role",
}


class Restricted(app_commands.CheckFailure):
    """Raised by the global tree check when a Restricted member runs anything.

    A distinct type so the error handler can say *why* — "you're restricted in
    this server" is a different message from "you don't have permission", and
    telling somebody the wrong one sends them to ask for a role they already
    would not be able to use.
    """


def tiers_at_or_above(config_key: str) -> tuple[str, ...]:
    """The tiers that satisfy a check for ``config_key``, highest first.

    The ladder in one place. A check for Verified is satisfied by Verified or
    Admin; a check for Admin only by Admin.
    """
    try:
        index = TIER_LADDER.index(config_key)
    except ValueError:
        # Not a laddered tier (Restricted, or a key from somewhere else). It
        # answers only for itself.
        return (config_key,)
    return TIER_LADDER[: index + 1]


@dataclass(frozen=True)
class PrivilegeResult:
    """One privilege decision, and the reason for it.

    ``allowed`` is the answer the checks act on; everything else exists so the
    answer can be explained. ``consulted_roles`` is false whenever the configured
    list never came into it -- which is the single most confusing case, because
    an administrator passing looks exactly like a role working.
    """

    allowed: bool
    reason: str
    matched_role: str | None = None
    # Which tier's list the matched role came from. Not always the tier that was
    # asked about: an Admin passing a Verified check matches on ``admin_roles``,
    # and reporting that as a Verified match would hide the implication that
    # actually decided it.
    matched_tier: str | None = None
    # The configured names for the tier asked about, and the member's own role
    # names. Both kept so a diagnostic can compare them without re-reading
    # anything.
    allowed_names: tuple[str, ...] = ()
    member_roles: tuple[str, ...] = ()
    # The tier the caller actually asked about, so by_implication has something
    # to compare matched_tier against.
    asked_tier: str | None = None

    @property
    def consulted_roles(self) -> bool:
        """Whether the configured list actually decided this."""
        return self.reason in (REASON_ROLE, REASON_NO_MATCH, REASON_RESTRICTED)

    @property
    def by_implication(self) -> bool:
        """Whether this passed on a *higher* tier than the one asked about."""
        return (
            self.reason == REASON_ROLE
            and self.matched_tier is not None
            and self.matched_tier != self.asked_tier
        )

    def describe(self) -> str:
        """A sentence for whoever is trying to work out what happened."""
        text = _REASON_TEXT.get(self.reason, self.reason)
        if self.reason in (REASON_ROLE, REASON_RESTRICTED):
            detail = f"{text} (`{self.matched_role}`)"
            if self.by_implication:
                label = TIER_LABELS.get(self.matched_tier, self.matched_tier)
                detail += f" — a {label} role, which passes this by implication"
            return detail
        return text


async def evaluate_privilege(
    bot,
    guild_id: int | None,
    user,
    config_key: str,
    *,
    member=None,
) -> PrivilegeResult:
    """Decide one privilege question and say why it came out that way.

    The single implementation behind every check in this module, so a diagnostic
    built on it cannot drift from the thing it claims to explain.

    The tiers imply downward: a check for ``config_key`` is satisfied by that
    tier's roles or by any tier above it (:func:`tiers_at_or_above`). Higher
    tiers are tried first, so ``matched_tier`` names the most senior list the
    member is on rather than whichever happened to be looked at first.

    Args:
        member: the resolved member, when the caller already has one. An
            interaction inside a guild carries roles on ``interaction.user``, so
            passing it avoids an HTTP fetch. Omit it and the member is looked up.
    """
    if await bot.is_owner(user):
        return PrivilegeResult(True, REASON_OWNER, asked_tier=config_key)
    if guild_id is None:
        return PrivilegeResult(False, REASON_NO_GUILD, asked_tier=config_key)

    if member is None:
        member = await _fetch_member(bot, guild_id, user)
        if member is None:
            return PrivilegeResult(False, REASON_NOT_A_MEMBER, asked_tier=config_key)

    config = bot.configs.get_guild(guild_id)
    # What the diagnostic reports is the tier that was *asked about*; the match
    # below ranges wider, and says so via matched_tier.
    allowed = tuple(config[config_key])
    held = tuple(role.name for role in member.roles)

    if member.guild_permissions.administrator:
        # Deliberately before the role check, and the reason is recorded: the
        # configured list is not consulted, so reporting this as a role match
        # would be the lie that makes two servers look inconsistent.
        return PrivilegeResult(
            True, REASON_ADMINISTRATOR, allowed_names=allowed, member_roles=held,
            asked_tier=config_key,
        )

    held_set = set(held)
    for tier in tiers_at_or_above(config_key):
        for name in config.get(tier, ()):
            if name in held_set:
                return PrivilegeResult(
                    True, REASON_ROLE, matched_role=name, matched_tier=tier,
                    allowed_names=allowed, member_roles=held, asked_tier=config_key,
                )
    return PrivilegeResult(
        False, REASON_NO_MATCH, allowed_names=allowed, member_roles=held,
        asked_tier=config_key,
    )


async def evaluate_restriction(
    bot,
    guild_id: int | None,
    user,
    *,
    member=None,
) -> PrivilegeResult:
    """Decide whether a member is Restricted in a server, and say why.

    ``allowed`` is **True when they are restricted** — this answers "does the
    deny apply", not "may they proceed". The global tree check inverts it.

    **Restricted overrides the ladder rather than sitting beneath it.** A member
    who holds both an Admin role and a restricted one is shut out: the tree check
    runs this before the command's own check, so the deny never has to out-argue
    a grant. That ordering is the whole rule, and getting it backwards fails open
    — the restricted Admin would keep everything.

    The exemptions match the positive tiers exactly: the bot owner and Discord
    server administrators are never restricted, and a role list cannot make them
    so. That is deliberate rather than incidental — a tier anybody with
    ``/verify`` access could point at a server administrator would be a way to
    lock the people who fix things out of fixing them.

    In a DM there is no server whose restriction could apply, so nobody is
    restricted. A DM-initiated flow that acts on one league has to ask this
    again with that league's guild id once it knows it -- ``/rumor`` is the one
    such flow, and does; see
    :func:`wojbot.cogs.rumors.refuse_if_restricted`. A command that is
    ``guild_only`` never has to think about this.
    """
    if await bot.is_owner(user):
        return PrivilegeResult(False, REASON_OWNER, asked_tier=RESTRICTED_ROLES_KEY)
    if guild_id is None:
        return PrivilegeResult(False, REASON_NO_GUILD, asked_tier=RESTRICTED_ROLES_KEY)

    if member is None:
        member = await _fetch_member(bot, guild_id, user)
        if member is None:
            # Not in the server, so its restrictions have nothing to bite on.
            return PrivilegeResult(
                False, REASON_NOT_A_MEMBER, asked_tier=RESTRICTED_ROLES_KEY
            )

    denied = tuple(bot.configs.get_guild(guild_id)[RESTRICTED_ROLES_KEY])
    held = tuple(role.name for role in member.roles)

    if member.guild_permissions.administrator:
        return PrivilegeResult(
            False, REASON_ADMINISTRATOR, allowed_names=denied, member_roles=held,
            asked_tier=RESTRICTED_ROLES_KEY,
        )

    denied_set = set(denied)
    matched = next((name for name in held if name in denied_set), None)
    if matched is not None:
        return PrivilegeResult(
            True, REASON_RESTRICTED, matched_role=matched,
            matched_tier=RESTRICTED_ROLES_KEY, allowed_names=denied,
            member_roles=held, asked_tier=RESTRICTED_ROLES_KEY,
        )
    return PrivilegeResult(
        False, REASON_NO_MATCH, allowed_names=denied, member_roles=held,
        asked_tier=RESTRICTED_ROLES_KEY,
    )


async def _fetch_member(bot, guild_id: int, user):
    """The member, over HTTP if the cache hasn't got them, or None.

    Fetching rather than trusting ``guild.get_member`` is deliberate. The bot
    runs on :meth:`discord.Intents.default`, which excludes the members intent,
    so the member cache is mostly empty and a cache miss would read as "no
    roles" -- silently denying somebody who is in fact privileged.
    """
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    member = guild.get_member(user.id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user.id)
    except discord.HTTPException:
        # Not in that server any more, or Discord is unhappy; either way this is
        # not somebody to hand privileged options to.
        return None


async def _has_role_privilege(interaction: discord.Interaction, config_key: str) -> bool:
    """True if the caller is owner, a server admin, or holds a role at/above the tier."""
    # interaction.user inside a guild is a Member and already carries its roles,
    # so this path never costs an HTTP fetch.
    result = await evaluate_privilege(
        interaction.client,
        interaction.guild_id if interaction.guild is not None else None,
        interaction.user,
        config_key,
        member=interaction.user if interaction.guild is not None else None,
    )
    return result.allowed


async def has_privilege_in_guild(bot, guild_id: int | None, user, config_key: str) -> bool:
    """The same tiers as :func:`_has_role_privilege`, against an explicit guild.

    The checks above read their roles off ``interaction.user``, which only
    carries them inside a guild -- in a DM they bail out and nobody but the
    owner passes. A flow that starts in a DM but *acts on* a particular league
    can still be gated: it resolves which server the league lives in and asks
    this, which fetches the caller's membership there.
    """
    result = await evaluate_privilege(bot, guild_id, user, config_key)
    return result.allowed


async def is_restricted_in_guild(bot, guild_id: int | None, user, *, member=None) -> bool:
    """Whether this member is Restricted in that server."""
    result = await evaluate_restriction(bot, guild_id, user, member=member)
    return result.allowed


async def is_admin_user(interaction: discord.Interaction) -> bool:
    """Predicate: does the caller have Admin privileges?"""
    return await _has_role_privilege(interaction, ADMIN_ROLES_KEY)


async def is_verified_user(interaction: discord.Interaction) -> bool:
    """Predicate: is the caller at least Verified? (Admin passes too.)"""
    return await _has_role_privilege(interaction, VERIFIED_ROLES_KEY)


async def is_bot_owner_user(interaction: discord.Interaction) -> bool:
    """Predicate: is the caller the bot's owner?

    The only tier that is not per-server — it gates settings that apply to every
    guild at once, which no single server's admin should be able to move.
    """
    return await interaction.client.is_owner(interaction.user)


async def is_server_administrator(interaction: discord.Interaction) -> bool:
    """Predicate: bot owner, or a Discord server administrator here.

    Deliberately *not* a configured tier — it is the one thing no role list can
    grant, which is what makes it the right gate for editing the role lists
    themselves. See ``/verify``.
    """
    if await interaction.client.is_owner(interaction.user):
        return True
    if interaction.guild is None:
        return False
    return interaction.user.guild_permissions.administrator


def is_admin():
    """Slash-command check decorator: Admin-only."""
    return app_commands.check(is_admin_user)


def is_verified():
    """Slash-command check decorator: Verified or above."""
    return app_commands.check(is_verified_user)


def is_bot_owner():
    """Slash-command check decorator: bot-owner-only."""
    return app_commands.check(is_bot_owner_user)


def requires_server_administrator():
    """Slash-command check decorator: owner or Discord server administrator."""
    return app_commands.check(is_server_administrator)
