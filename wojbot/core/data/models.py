"""Domain models for league/fantasy data held in the bot.

Kept intentionally small for now — this is the shape v1 loaded from SQL into
pandas tables (a league bound to a Discord guild via its channel/guild id, plus a
team_key -> owner map). Fields for elos, managers, standings, etc. get added when
the cogs that need them (commish, annuhlitucks/elo, rumors) are ported.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LeagueData:
    """A fantasy league and the bits needed to run bot commands for it."""

    league_id: int
    guild_id: int
    name: str | None = None
    dynasty: bool = False
    # team_key -> owner display name
    team_map: dict[str, str] = field(default_factory=dict)
