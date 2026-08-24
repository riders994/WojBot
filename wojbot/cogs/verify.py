"""Verify cog: manage which roles grant (or deny) command privileges.

The tier logic lives in :mod:`wojbot.core.checks` (reused by other cogs as
decorators). This cog is the management surface: view the four tiers and
add/remove roles, persisted per-server via ``bot.configs``.

**Editing a tier is itself a privileged act, and the gate is deliberately not
the tier being edited.** Granting Admin is gated on being a Discord server
administrator rather than on Admin, because a tier that can hand itself out is
not a tier -- under the old two-tier cog the lower list could add itself to the
higher one, so anybody with ``mods`` could make themselves commissioner. The
ladder makes that worse rather than better, so the gate moved to the one thing
no configured role can grant.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..core.checks import (
    ADMIN_ROLES_KEY,
    RESTRICTED_ROLES_KEY,
    TIER_LABELS,
    TIER_LADDER,
    evaluate_privilege,
    is_admin,
    is_server_administrator,
)

# Every editable tier, highest first, then the deny tier.
_EDITABLE_TIERS = TIER_LADDER + (RESTRICTED_ROLES_KEY,)

_TIER_CHOICES = [
    app_commands.Choice(name=TIER_LABELS[key], value=key) for key in _EDITABLE_TIERS
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

    async def _may_edit(self, interaction: discord.Interaction, tier: str) -> bool:
        """Whether the caller may edit ``tier``'s role list.

        Admin is gated on server administrator (see the module docstring);
        everything below it on Admin. Restricted sits with the lower tiers on
        purpose -- an Admin needs to be able to shut down abuse without waiting
        for somebody with Discord's Administrator bit.
        """
        if tier == ADMIN_ROLES_KEY:
            return await is_server_administrator(interaction)
        return True  # the @is_admin() decorator already established this much

    async def _refuse(self, interaction: discord.Interaction, tier: str) -> None:
        await interaction.response.send_message(
            f"Only a Discord **server administrator** can change the "
            f"{TIER_LABELS[tier]} roles — that tier runs the league, so it "
            "isn't something the bot's own roles can hand out.",
            ephemeral=True,
        )

    @group.command(name="show", description="Show permission roles and your own status.")
    async def show(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.configs.get_guild(interaction.guild_id)
        lines = []
        for key in _EDITABLE_TIERS:
            names = ", ".join(f"`{r}`" for r in cfg[key]) or "(none)"
            lines.append(f"**{TIER_LABELS[key]} roles:** {names}")

        # Ask about each tier through the same code the checks use, so what this
        # reports cannot drift from what actually happens. describe() is what
        # separates "your role worked" from "you're a server admin, and the
        # roles were never read" -- the two that look identical from outside.
        standing = []
        for key in TIER_LADDER:
            result = await evaluate_privilege(
                self.bot, interaction.guild_id, interaction.user,
                key, member=interaction.user,
            )
            mark = "✅" if result.allowed else "❌"
            standing.append(f"{mark} **{TIER_LABELS[key]}** — {result.describe()}")

        await interaction.response.send_message(
            "\n".join(lines)
            + "\n\nThe tiers are a ladder: Admin passes every Verified check. "
            "Restricted overrides all of it.\n\n**Where you stand**\n"
            + "\n".join(standing),
            ephemeral=True,
        )

    @group.command(name="add", description="Grant a role a permission tier.")
    @app_commands.describe(tier="Which permission tier", role="Role to grant")
    @app_commands.choices(tier=_TIER_CHOICES)
    @is_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        role: discord.Role,
    ) -> None:
        if not await self._may_edit(interaction, tier.value):
            await self._refuse(interaction, tier.value)
            return
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
    @is_admin()
    async def remove(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        role: discord.Role,
    ) -> None:
        if not await self._may_edit(interaction, tier.value):
            await self._refuse(interaction, tier.value)
            return
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
