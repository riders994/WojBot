"""Annuhlitucks cog: read-only display of the league's Elo ratings.

The v1 cog scraped and computed ratings on demand. In v2 that work belongs to
the commish cog (``/commish sync|publish``); annuhlitucks is purely the display
surface over the ratings frames — public, no gating.

It reads through the shared :class:`elo_system.EloSystem` for the guild's league
(see :mod:`wojbot.core.elo`), so it reuses whatever the commish cog loaded. The
ratings frames are member-indexed with ``week_N`` columns; a member's current
rating is their latest populated week.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..core.elo import get_or_build

log = logging.getLogger(__name__)

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _latest_series(frame):
    """Latest populated rating per member, sorted high to low (or None)."""
    if frame is None or getattr(frame, "empty", True):
        return None
    return frame.ffill(axis=1).iloc[:, -1].sort_values(ascending=False)


class Annuhlitucks(commands.Cog):
    """League Elo ratings and standings."""

    group = app_commands.Group(
        name="elo",
        description="League Elo ratings.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _system(self, interaction: discord.Interaction):
        """Resolve (building if needed) this guild's EloSystem, or report why not."""
        try:
            return await get_or_build(self.bot, interaction.guild_id)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the user
            log.exception("Failed to resolve Elo system")
            await interaction.followup.send(
                f"Couldn't load this server's league: `{exc}`\n"
                "A commissioner may need to run `/commish load` first."
            )
            return None

    @group.command(name="standings", description="Show current Elo standings.")
    @app_commands.describe(
        season="Season year (defaults to the current one)",
        dynasty="Show the all-time dynasty ratings instead of one season",
    )
    async def standings(
        self,
        interaction: discord.Interaction,
        season: int | None = None,
        dynasty: bool = False,
    ) -> None:
        await interaction.response.defer()
        system = await self._system(interaction)
        if system is None:
            return

        def _work():
            league = system.elo_league
            if league.frame_manager is None:
                system.load_frames(None)
            fm = league.frame_manager
            if dynasty:
                return "dynasty", _latest_series(fm.dynasty_elo)
            seasons = fm.seasonal_elo
            year = season
            if year is None:
                current = (system.ratings_config or {}).get("current_sports_year")
                year = current if current in seasons else (max(seasons) if seasons else None)
            if year not in seasons:
                raise ValueError(
                    f"No ratings for season {season}. Available: "
                    + (", ".join(str(y) for y in sorted(seasons)) or "none")
                )
            return str(year), _latest_series(seasons[year])

        try:
            label, series = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the user
            await interaction.followup.send(f"Couldn't build standings: `{exc}`")
            return
        if series is None or series.empty:
            await interaction.followup.send(f"No ratings recorded for {label} yet.")
            return

        name = (system.ratings_config or {}).get("league_name", system.league)
        lines = [
            f"{MEDALS.get(rank, f'`{rank:>2}`')} **{member}** — {rating:.0f}"
            for rank, (member, rating) in enumerate(series.items(), start=1)
        ]
        embed = discord.Embed(
            title=f"{name} — {label} standings",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await interaction.followup.send(embed=embed)

    @group.command(name="team", description="Show one member's current Elo.")
    @app_commands.describe(
        member="Team/manager name as it appears in the ratings",
        season="Season year (defaults to the current one)",
        dynasty="Look up the dynasty rating instead of one season",
    )
    async def team(
        self,
        interaction: discord.Interaction,
        member: str,
        season: int | None = None,
        dynasty: bool = False,
    ) -> None:
        await interaction.response.defer()
        system = await self._system(interaction)
        if system is None:
            return

        def _work():
            league = system.elo_league
            if league.frame_manager is None:
                system.load_frames(None)
            fm = league.frame_manager
            if dynasty:
                frame, label = fm.dynasty_elo, "dynasty"
            else:
                seasons = fm.seasonal_elo
                year = season
                if year is None:
                    current = (system.ratings_config or {}).get("current_sports_year")
                    year = current if current in seasons else (max(seasons) if seasons else None)
                if year not in seasons:
                    raise ValueError(
                        f"No ratings for season {season}. Available: "
                        + (", ".join(str(y) for y in sorted(seasons)) or "none")
                    )
                frame, label = seasons[year], str(year)
            series = _latest_series(frame)
            if series is None:
                return label, None
            # Match the member name case-insensitively: exact first, else a
            # unique substring.
            query = member.strip().lower()
            labels = [str(i) for i in series.index]
            match = next((series.index[i] for i, n in enumerate(labels) if n.lower() == query), None)
            if match is None:
                hits = [series.index[i] for i, n in enumerate(labels) if query in n.lower()]
                if len(hits) == 1:
                    match = hits[0]
                elif len(hits) > 1:
                    raise ValueError("Matches several members: " + ", ".join(str(h) for h in hits))
            if match is None:
                raise ValueError(f"No member matching '{member}'.")
            rating = float(series.loc[match])
            rank = list(series.index).index(match) + 1
            # Change since the previous recorded week.
            row = frame.loc[match].dropna()
            delta = float(row.iloc[-1] - row.iloc[-2]) if len(row) >= 2 else 0.0
            return label, (str(match), rating, rank, len(series), delta)

        try:
            label, result = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the user
            await interaction.followup.send(f"Couldn't look that up: `{exc}`")
            return
        if result is None:
            await interaction.followup.send(f"No ratings recorded for {label} yet.")
            return

        matched, rating, rank, total, delta = result
        trend = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
        name = (system.ratings_config or {}).get("league_name", system.league)
        embed = discord.Embed(
            title=f"{matched} — {label}",
            description=(
                f"**Elo:** {rating:.0f}  ({trend} {abs(delta):.0f} last week)\n"
                f"**Rank:** {rank} of {total}"
            ),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=f"{name}")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Annuhlitucks(bot))
