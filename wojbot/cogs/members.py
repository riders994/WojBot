"""Members cog: small, self-contained member-info commands.

Ported from v1's ``MembersCog``. These work on any server (they don't touch
league data) and are grouped under ``/member``. Each takes an optional ``user``
and defaults to the caller. Modernized for discord.py 2.x: Discord-native
timestamps instead of raw ``datetime`` text, and ``display_avatar`` instead of
the removed ``avatar_url``.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class Members(commands.Cog):
    """Info about server members."""

    group = app_commands.Group(
        name="member",
        description="Look up info about a server member.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @group.command(name="joined", description="Show when a member joined the server.")
    @app_commands.describe(user="Member to look up (defaults to you).")
    async def joined(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        member = user or interaction.user
        if member.joined_at is None:
            await interaction.response.send_message(
                f"{member.display_name}'s join date isn't available.", ephemeral=True
            )
            return
        stamp = discord.utils.format_dt(member.joined_at, style="F")
        relative = discord.utils.format_dt(member.joined_at, style="R")
        await interaction.response.send_message(
            f"{member.display_name} joined on {stamp} ({relative})."
        )

    @group.command(name="toprole", description="Show a member's highest role.")
    @app_commands.describe(user="Member to look up (defaults to you).")
    async def toprole(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        member = user or interaction.user
        await interaction.response.send_message(
            f"The top role for {member.display_name} is **{member.top_role.name}**."
        )

    @group.command(name="roles", description="List a member's roles.")
    @app_commands.describe(user="Member to look up (defaults to you).")
    async def roles(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        member = user or interaction.user
        roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
        listed = ", ".join(roles) if roles else "(no roles)"
        await interaction.response.send_message(
            f"Roles for {member.display_name}: {listed}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="perms", description="Show a member's server permissions.")
    @app_commands.describe(user="Member to look up (defaults to you).")
    async def perms(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        member = user or interaction.user
        granted = [
            perm.replace("_", " ").title()
            for perm, value in member.guild_permissions
            if value
        ]
        embed = discord.Embed(
            title="Permissions",
            description=", ".join(granted) if granted else "(none)",
            colour=member.colour,
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Members(bot))
