"""Default configuration for each settings scope.

Defaults live in code (not in the YAML files). A scope's stored file holds only
the *overrides*; reads merge those over these defaults, so adding a new default
here automatically applies to every server/league without a data migration.
"""

from __future__ import annotations

# Global, bot-wide settings. Owner-only, and edited through /setup global.
DEFAULT_BOT_CONFIG: dict = {
    # The dad-joke setting a server starts on before it picks its own.
    "dad_joke": False,
}

# Server settings whose default comes from the bot config rather than from
# DEFAULT_SERVER_CONFIG. A server that has set its own value always keeps it;
# this only decides what one that never has follows, so the owner can move the
# fleet without overriding anybody's choice. See ConfigStore.get_guild.
INHERITED_FROM_BOT: tuple[str, ...] = ("dad_joke",)

# Per-Discord-server (guild) settings.
DEFAULT_SERVER_CONFIG: dict = {
    # Falls back to the bot-wide setting before this one; see INHERITED_FROM_BOT.
    "dad_joke": False,
    # Role names that grant command privileges (see wojbot.core.checks). The bot
    # owner and Discord server administrators always pass regardless of these.
    "commissioner_roles": ["Commish"],
    "admin_roles": ["mods", "Champion", "Commish"],
    # Which configured Elo league (a key in resources/configs/sys_config.yml)
    # this server drives. Bound by /commish load; None until a commish loads one.
    "elo_league": None,
    # Channel id reported rumors get posted to. None until /setup rumorchannel
    # names one, and rumors reported before then are still recorded -- they just
    # go unannounced.
    "rumor_channel": None,
}

# Per-fantasy-league settings.
DEFAULT_LEAGUE_CONFIG: dict = {
    "dynasty": False,
}
