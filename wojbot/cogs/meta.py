"""Meta cog: health check, bot info, and the in-Discord help.

This is the first v2 cog and the template every ported cog should follow —
a ``commands.Cog`` subclass with ``@app_commands.command()`` slash commands and
an ``async def setup(bot)`` entry point.

``/help`` is the short version of ``docs/getting-started.md``. The two are meant
to say the same things, so a change to how setup works belongs in both — the doc
has the room to explain, this has the answer somebody needs mid-conversation.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from wojbot import __version__

DOCS_PATH = "docs/getting-started.md"

# topic -> (title, body). Kept as data so adding one is a dict entry plus a
# Choice, and the overview's topic list stays in step with what exists.
TOPICS: dict[str, tuple[str, str]] = {
    "setup": (
        "Setting up a league server",
        "Run these **in the league's Discord server**, in order. Only server "
        "administrators and the bot owner can, until roles are configured.\n\n"
        "**1. `/setup wizard`**\nRoles, which league this server drives, the "
        "rumor channel, dad jokes. Every step is optional and it can be re-run. "
        "The rumor step only appears once the server is bound to a league — "
        "that's what rumors belong to.\n\n"
        "**2. `/commish db migrate`**\nAdopts the league's existing database row "
        "for this server. Needs the league loaded first, so do it after the "
        "wizard. Prints the roster.\n\n"
        "**3. `/commish db managers` → `/commish db linkuser`**\nAttach each "
        "Discord user to their manager, one at a time — a manager's identity in "
        "the database is their Fantrax/Sleeper account, which can't be matched "
        "to a Discord user automatically. **Until somebody is linked they can't "
        "report rumors.**\n\n"
        "**4. `/setup show`**\nThis server's settings and everything still "
        "outstanding, each with the command that fixes it."
    ),
    "rumors": (
        "Reporting rumors",
        "`/rumor report` — file one. `/rumor recent` — read the last few "
        "(`count:` for more; the season is the ceiling).\n\n"
        "**Reporting works in a DM as well as in the server**, which is rather "
        "the point. In a DM the league is worked out from who you are; if you "
        "play in more than one **this season**, you're asked which. Leagues "
        "you've left aren't offered — you can't speak for a club you no longer "
        "run.\n\n"
        "The wizard asks for a **release type** (how loudly it's being said), a "
        "**source type** (who's saying it) and a **form** (the sentence it goes "
        "in), then the text. Your team and handle fill themselves in.\n\n"
        "**The heavier the release, the more senior a source it takes.** A "
        "*statement* has to come from your GM or President of Basketball Ops; a "
        "*league release* only from the Commissioner, which is Verified-tier. A "
        "plain *rumor* will take anybody, down to *anonymous sources*. Releases "
        "you have no source for aren't offered.\n\n"
        "The post **never names you** — the source type is the attribution.\n\n"
        "Rumors go to the channel `/setup rumorchannel` names. Without one they "
        "are still recorded, they just go out to nobody."
    ),
    "elo": (
        "Elo ratings",
        "**Anyone:** `/elo standings` and `/elo team`.\n\n"
        "**Commissioner:** `/elo run` recomputes a week or a range. "
        "`/commish load` binds a league to this server, "
        "`/commish sync` scrapes members and teams into the database, "
        "`/commish publish` runs and publishes ratings, `/commish dump` backs up "
        "the config files, and `/commish status` says what's loaded.\n\n"
        "`/commish season add|list|current|platform` owns the league's "
        "league-years. A season only reaches the database on the next sync or "
        "publish."
    ),
    "permissions": (
        "Permission tiers",
        "**Four tiers, and they're a ladder — each one passes everything below "
        "it.**\n\n"
        "**Admin** — `admin_roles`, default `Commish`. Runs the league: "
        "`/commish load|sync|publish|dump`, all of `/commish db`, "
        "`/commish season add|platform|current`, and `/verify add|remove`.\n\n"
        "**Verified** — `verified_roles`, default `mods`, `Champion`. Trusted "
        "to deputise when the commissioner is out: `/setup`, the read-only "
        "`/commish` commands, and the Commissioner source in `/rumor`. Admins "
        "pass all of this without being listed here.\n\n"
        "**Normal** — everybody else, and the default. Not a role list: it's "
        "simply every command that has no tier on it.\n\n"
        "**Restricted** — `restricted_roles`, empty by default. The only tier "
        "that takes something away. A restricted member is refused *every* "
        "command bar `/help` and `/verify show`, whatever else they hold — it "
        "overrides the ladder rather than sitting under it.\n\n"
        "**Outside the ladder.** The bot owner passes every check everywhere, "
        "and gates `/sql` and `/setup global`. Discord server administrators "
        "pass every tier in their own server and can't be restricted — which is "
        "also why only they can change the **Admin** list. An Admin can edit "
        "Verified and Restricted, but not the tier they're standing on.\n\n"
        "Roles are matched **by name** and exactly, per server, so renaming a "
        "role in Discord silently unconfigures it. A role belongs in one list, "
        "the highest that should hold it — listing it twice just means somebody "
        "will remove it from one and assume the other still covers it.\n\n"
        "`/verify show` tells you where you stand, and says *why* for each "
        "tier: passing as a server administrator (roles never read), on your "
        "own tier's role, or on an Admin role by implication. Those look "
        "identical from outside and are not — so check with somebody who is "
        "neither owner nor server admin before concluding a role works."
    ),
}

TOPIC_CHOICES = [
    app_commands.Choice(name="Setting up a league server", value="setup"),
    app_commands.Choice(name="Reporting rumors", value="rumors"),
    app_commands.Choice(name="Elo ratings", value="elo"),
    app_commands.Choice(name="Permission tiers", value="permissions"),
]

OVERVIEW = (
    "A bot for a fantasy basketball league.\n\n"
    "**`/rumor`** — report a rumor, or read the recent ones. Works in a DM.\n"
    "**`/elo`** — standings and ratings.\n"
    "**`/setup`** — configure this server. Start with `/setup wizard`.\n"
    "**`/commish`** — commissioner tools: leagues, seasons, database.\n"
    "**`/verify`** — who is allowed to run what.\n"
    "**`/member`**, **`/roll`**, **`/cat`**, **`/ping`** — odds and ends.\n\n"
    "Pick a `topic` for more, or run `/setup show` to see what this server "
    f"still needs. The full guide is `{DOCS_PATH}` in the repo."
)


class Meta(commands.Cog):
    """Basic health, info, and help commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Check that the bot is alive and see its latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! ({latency_ms} ms)")

    @app_commands.command(description="Show basic information about the bot.")
    async def about(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"**WojBot** v{__version__} — a fantasy basketball league bot. "
            "Try `/help`."
        )

    @app_commands.command(description="How to use the bot, and how to set it up.")
    @app_commands.describe(topic="Which part to explain (omit for the overview)")
    @app_commands.choices(topic=TOPIC_CHOICES)
    async def help(
        self, interaction: discord.Interaction, topic: app_commands.Choice[str] | None = None
    ) -> None:
        if topic is None:
            title, body = f"WojBot v{__version__}", OVERVIEW
        else:
            title, body = TOPICS[topic.value]
        embed = discord.Embed(
            title=title, description=body, colour=discord.Colour.blurple()
        )
        # Ephemeral: help is for whoever asked, and this one runs long enough
        # that a channel would rather not carry it.
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Meta(bot))
