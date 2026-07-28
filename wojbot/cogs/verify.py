"""Verify cog: manage which roles grant command privileges.

The verification logic lives in :mod:`wojbot.core.checks` (reused by other cogs as
decorators). This cog is the management surface: view the configured tiers and
add/remove roles, persisted per-server via ``bot.configs``.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..core.checks import (
    ADMIN_ROLES_KEY,
    COMMISSIONER_ROLES_KEY,
    is_commissioner_user,
    is_privileged,
    is_privileged_user,
)

_TIER_CHOICES = [
    app_commands.Choice(name="Commissioner", value=COMMISSIONER_ROLES_KEY),
    app_commands.Choice(name="Admin", value=ADMIN_ROLES_KEY),
]


class Verify(commands.Cog):
    """Configure command permissions for this server."""

    group = app_commands.Group(
        name="verify",
        description="Manage command permission roles.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @group.command(name="show", description="Show permission roles and your own status.")
    async def show(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.configs.get_guild(interaction.guild_id)
        commissioner = ", ".join(f"`{r}`" for r in cfg[COMMISSIONER_ROLES_KEY]) or "(none)"
        admin = ", ".join(f"`{r}`" for r in cfg[ADMIN_ROLES_KEY]) or "(none)"
        is_comm = await is_commissioner_user(interaction)
        is_priv = await is_privileged_user(interaction)
        await interaction.response.send_message(
            f"**Commissioner roles:** {commissioner}\n"
            f"**Admin roles:** {admin}\n\n"
            f"Your access — commissioner: {'✅' if is_comm else '❌'} · "
            f"admin: {'✅' if is_priv else '❌'}",
            ephemeral=True,
        )

    @group.command(name="add", description="Grant a role a permission tier.")
    @app_commands.describe(tier="Which permission tier", role="Role to grant")
    @app_commands.choices(tier=_TIER_CHOICES)
    @is_privileged()
    async def add(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        role: discord.Role,
    ) -> None:
        roles = list(self.bot.configs.get_guild(interaction.guild_id)[tier.value])
        if role.name in roles:
            await interaction.response.send_message(
                f"`{role.name}` is already a {tier.name} role.", ephemeral=True
            )
            return
        roles.append(role.name)
        self.bot.configs.set_guild(interaction.guild_id, {tier.value: roles})
        await interaction.response.send_message(
            f"Added `{role.name}` to {tier.name} roles.", ephemeral=True
        )

    @group.command(name="remove", description="Revoke a role's permission tier.")
    @app_commands.describe(tier="Which permission tier", role="Role to revoke")
    @app_commands.choices(tier=_TIER_CHOICES)
    @is_privileged()
    async def remove(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        role: discord.Role,
    ) -> None:
        roles = list(self.bot.configs.get_guild(interaction.guild_id)[tier.value])
        if role.name not in roles:
            await interaction.response.send_message(
                f"`{role.name}` is not a {tier.name} role.", ephemeral=True
            )
            return
        roles.remove(role.name)
        self.bot.configs.set_guild(interaction.guild_id, {tier.value: roles})
        await interaction.response.send_message(
            f"Removed `{role.name}` from {tier.name} roles.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Verify(bot))
