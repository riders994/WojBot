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

The cog also owns the other half of a restart: announcing one. Coming back up
it DMs the owner a line drawn at random from ``resources/restart_messages.json``
-- from the *commanded* list if ``/restart`` left a marker behind, from the
*disruption* list if nothing did -- and posts that same line to every server
that has subscribed with ``/setup restartnotices``. See
:mod:`wojbot.core.restart`.

The owner always hears; a server hears only if it asked to. One line is drawn
per restart and reused, because one restart is one event: a bot that told the
owner it was ordered down and a server it fell over would be describing two
different nights.
"""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from ..core.checks import is_bot_owner
from ..core.restart import (
    mark_commanded,
    notify_targets,
    pick_message,
    reason_from_marker,
    take_marker,
)

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
        # Left for the *next* process to find: this one won't be here to say
        # why it went. Written before the close so a shutdown that hangs still
        # gets reported as the deliberate thing it was.
        mark_commanded(interaction.user.id)
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
        # on_ready fires again on every gateway resume, and a reconnect is not
        # a restart. One announcement per process, so the flag lives here.
        self._announced = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._announced:
            return
        self._announced = True
        reason = reason_from_marker(take_marker())
        # Drawn once, said everywhere. See the module docstring.
        message = pick_message(reason)
        log.info("Announcing a %s restart", reason)
        await self._tell_owner(message)
        await self._tell_servers(message)

    async def _tell_owner(self, message: str) -> None:
        """DM the bot owner. Never raises: an unannounced start still ran."""
        try:
            app = self.bot.application or await self.bot.application_info()
            owner = app.owner
            if owner is None:
                log.warning("No owner on the application; restart went unannounced")
                return
            await owner.send(message)
        except discord.Forbidden:
            log.warning("The owner's DMs are closed; restart went unannounced")
        except Exception:  # noqa: BLE001 - announcing must never take the bot down
            log.exception("Couldn't tell the owner about the restart")

    async def _tell_servers(self, message: str) -> None:
        """Post to every subscribed server's default channel.

        Each server is tried on its own and its failure kept to itself: a
        channel deleted in one server must not cost the announcement in the
        others, and none of it may cost the start. Who to tell is
        :func:`notify_targets`; this is only the telling.
        """
        targets = notify_targets(self.bot.configs, [g.id for g in self.bot.guilds])
        for guild_id, channel_id in targets:
            try:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                await channel.send(message)
            except discord.Forbidden:
                # Configured, still there, and shut to the bot. Worth a line:
                # the server asked for these and is not getting them.
                log.warning(
                    "Can't post restart notices in channel %s (server %s)",
                    channel_id, guild_id,
                )
            except discord.NotFound:
                log.warning(
                    "The default channel for server %s (`%s`) is gone; "
                    "restart notice undelivered", guild_id, channel_id,
                )
            except Exception:  # noqa: BLE001 - one server must not cost the rest
                log.exception(
                    "Couldn't announce the restart to server %s", guild_id
                )
        if targets:
            log.info("Announced the restart to %d server(s)", len(targets))

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
