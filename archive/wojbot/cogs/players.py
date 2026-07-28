from discord.ext import commands


class Players(commands.Cog, name='Player Commands'):
    """
    Commands associated with players. These commands are not available outside of leagues. They will yield errors.
    """
    def __init__(self, bot):
        self.bot = bot


def setup(bot):
    bot.add_cog(Players(bot))
