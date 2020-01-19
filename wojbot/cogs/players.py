from discord.ext import commands


class Players(commands.Cog, name='Player Commands'):
    def __init__(self, bot):
        self.bot = bot


def setup(bot):
    bot.add_cog(Players(bot))
