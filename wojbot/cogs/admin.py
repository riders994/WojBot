"""Owner-only control of the bot process itself.

``/restart`` shuts the bot down. Nothing here starts it again -- systemd does,
and that is the whole point: both deployments run the bot under a unit with
``Restart=always`` and ``ExecStartPre=-/usr/local/bin/wojbot-update``, so the
supervisor answers a clean exit by pulling the branch, installing against
``requirements.lock``, and starting the new code. Exiting from inside is
therefore the same operation as ``systemctl restart wojbot``, reached without a
sudoers entry for the service account. See ``docs/deploying-to-a-pi.md`` and
``docs/deploying-to-ec2.md``.

Which is also why the command refuses when nothing is supervising the process:
run straight from a shell, ``/restart`` is just ``/quit``.
"""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from ..core.checks import is_bot_owner

log = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 60.0


def supervised(env: dict[str, str] | None = None) -> bool:
    """Whether something will start the bot again after it exits.

    ``INVOCATION_ID`` is set by systemd in the environment of every service it
    runs, and by nothing else -- it is the one thing a unit-managed process can
    see about its own supervision without asking the system bus.
    """
    return "INVOCATION_ID" in (os.environ if env is None else env)


class ConfirmRestart(discord.ui.View):
    """The one button that ends the process.

    Owned by whoever ran the command; the owner check on ``/restart`` already
    ran, so this only stops a second owner clicking someone else's prompt.
    """

    def __init__(self, bot: commands.Bot, user: discord.abc.User) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.bot = bot
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This one belongs to whoever ran it.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        self.clear_items()

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.clear_items()
        self.stop()
        # Answered before the shutdown, not after: closing tears down the HTTP
        # session this reply goes out on.
        await interaction.response.edit_message(content="Restarting.", view=self)
        log.warning(
            "Restart requested by %s (%s); shutting down", interaction.user, interaction.user.id
        )
        # Bot.close unloads the extensions on the way out, so the cogs holding
        # something open -- the database connection, the scheduler's tick --
        # get their cog_unload rather than having the process pulled from under
        # them. It returns to bot.run(), which returns to main(), which exits 0.
        await self.bot.close()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.clear_items()
        self.stop()
        await interaction.response.edit_message(content="Left running.", view=self)


class Admin(commands.Cog):
    """Owner-only commands that act on the bot process."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Restart the bot (owner only).")
    @is_bot_owner()
    async def restart(self, interaction: discord.Interaction) -> None:
        if not supervised():
            await interaction.response.send_message(
                "I'm not running under systemd here, so nothing would start me "
                "back up — restart me the way you started me.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Restart the bot?", view=ConfirmRestart(self.bot, interaction.user), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
