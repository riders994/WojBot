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


async def is_commissioner_user(interaction: discord.Interaction) -> bool:
    """Predicate: does the caller have commissioner privileges?"""
    return await _has_role_privilege(interaction, COMMISSIONER_ROLES_KEY)


async def is_privileged_user(interaction: discord.Interaction) -> bool:
    """Predicate: does the caller have admin/privileged access?"""
    return await _has_role_privilege(interaction, ADMIN_ROLES_KEY)


def is_commissioner():
    """Slash-command check decorator: commissioner-only."""
    return app_commands.check(is_commissioner_user)


def is_privileged():
    """Slash-command check decorator: admin/privileged-only."""
    return app_commands.check(is_privileged_user)
