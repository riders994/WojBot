"""Max-PF cog: offers the weekly max-points-for report.

**All this cog does is register a weekly process**, which is why it has no
commands. The pattern is the one the Elo cog follows for the Elo process:
whichever cog owns a feature registers that feature's process with
``bot.weekly``, because cogs load in an arbitrary order and the cog that
schedules them (``cogs/weekly.py``) can't reach back for something it doesn't
own. The work itself is :mod:`wojbot.core.maxpf`.

``max_pf`` is imported inside the run, not here. A bot whose install is missing
it should still load, still list the process, and record the ImportError against
the run that needed it -- a process that silently isn't offered looks exactly
like one nobody added.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from ..core.maxpf import maxpf_process

log = logging.getLogger(__name__)


class MaxPf(commands.Cog):
    """Registers the weekly max-PF report. See ``/weekly``."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        bot.weekly.register(maxpf_process())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MaxPf(bot))
