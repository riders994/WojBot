"""Reusable permission checks for slash commands.

Other cogs gate commands by decorating them::

    from ..core.checks import is_commissioner

    @app_commands.command()
    @is_commissioner()
    async def dump(self, interaction): ...

A user passes a check if they are the **bot owner**, a **Discord server
administrator**, or hold one of the role names configured for that tier in the
guild's settings (``commissioner_roles`` / ``admin_roles`` in
:data:`wojbot.core.defaults.DEFAULT_SERVER_CONFIG`, editable via the ``/verify``
cog). Role matching is by name, matching v1 and keeping the YAML config
human-readable — renaming a role in Discord means updating it here too.

A failed check raises :class:`discord.app_commands.CheckFailure`, which the bot's
tree error handler turns into a friendly ephemeral reply.

The decorators are all guild-bound -- they read roles off the interaction, which
has none in a DM. :func:`has_privilege_in_guild` is the way to apply a server's
roles from outside it, for a flow that runs in a DM but acts on one league.
"""

from __future__ import annotations

import discord
from discord import app_commands

COMMISSIONER_ROLES_KEY = "commissioner_roles"
ADMIN_ROLES_KEY = "admin_roles"


async def _has_role_privilege(interaction: discord.Interaction, config_key: str) -> bool:
    """True if the caller is owner, a server admin, or holds a configured role."""
    # Bot owner always passes.
    if await interaction.client.is_owner(interaction.user):
        return True
    # Everything below needs a guild member.
    if interaction.guild is None:
        return False
    member = interaction.user
    # Discord server administrators always pass.
    if member.guild_permissions.administrator:
        return True
    allowed = set(interaction.client.configs.get_guild(interaction.guild_id)[config_key])
    return any(role.name in allowed for role in member.roles)


async def has_privilege_in_guild(bot, guild_id: int | None, user, config_key: str) -> bool:
    """The same tiers as :func:`_has_role_privilege`, against an explicit guild.

    The checks above read their roles off ``interaction.user``, which only
    carries them inside a guild -- in a DM they bail out and nobody but the
    owner passes. A flow that starts in a DM but *acts on* a particular league
    can still be gated: it resolves which server the league lives in and asks
    this, which fetches the caller's membership there.

    Fetching over HTTP rather than trusting ``guild.get_member`` is deliberate.
    The bot runs on :meth:`discord.Intents.default`, which excludes the members
    intent, so the member cache is mostly empty and a cache miss would read as
    "no roles" -- silently denying somebody who is in fact privileged.
    """
    if await bot.is_owner(user):
        return True
    if guild_id is None:
        return False
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    member = guild.get_member(user.id)
    if member is None:
        try:
            member = await guild.fetch_member(user.id)
        except discord.HTTPException:
            # Not in that server any more, or Discord is unhappy; either way
            # this is not somebody to hand privileged options to.
            return False
    if member.guild_permissions.administrator:
        return True
    allowed = set(bot.configs.get_guild(guild_id)[config_key])
    return any(role.name in allowed for role in member.roles)


async def is_commissioner_user(interaction: discord.Interaction) -> bool:
    """Predicate: does the caller have commissioner privileges?"""
    return await _has_role_privilege(interaction, COMMISSIONER_ROLES_KEY)


async def is_privileged_user(interaction: discord.Interaction) -> bool:
    """Predicate: does the caller have admin/privileged access?"""
    return await _has_role_privilege(interaction, ADMIN_ROLES_KEY)


async def is_bot_owner_user(interaction: discord.Interaction) -> bool:
    """Predicate: is the caller the bot's owner?

    The only tier that is not per-server — it gates settings that apply to every
    guild at once, which no single server's admin should be able to move.
    """
    return await interaction.client.is_owner(interaction.user)


def is_commissioner():
    """Slash-command check decorator: commissioner-only."""
    return app_commands.check(is_commissioner_user)


def is_privileged():
    """Slash-command check decorator: admin/privileged-only."""
    return app_commands.check(is_privileged_user)


def is_bot_owner():
    """Slash-command check decorator: bot-owner-only."""
    return app_commands.check(is_bot_owner_user)
