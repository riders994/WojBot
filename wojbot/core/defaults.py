"""Default configuration for each settings scope.

Defaults live in code (not in the YAML files). A scope's stored file holds only
the *overrides*; reads merge those over these defaults, so adding a new default
here automatically applies to every server/league without a data migration.
"""

from __future__ import annotations

# Global, bot-wide settings.
DEFAULT_BOT_CONFIG: dict = {}

# Per-Discord-server (guild) settings.
DEFAULT_SERVER_CONFIG: dict = {
    "dad_joke": False,
    # Role names that grant command privileges (see wojbot.core.checks). The bot
    # owner and Discord server administrators always pass regardless of these.
    "commissioner_roles": ["Commish"],
    "admin_roles": ["mods", "Champion", "Commish"],
}

# Per-fantasy-league settings.
DEFAULT_LEAGUE_CONFIG: dict = {
    "dynasty": False,
}
