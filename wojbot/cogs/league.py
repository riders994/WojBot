from discord.ext import commands


class League(commands.Cog, name='League Commands'):
    def __init__(self, bot):
        self.bot = bot


def setup(bot):
    bot.add_cog(League(bot))
