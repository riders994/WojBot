from discord.ext import commands


class Rumors(commands.Cog, name='Rumor Commands'):
    def __init__(self, bot):
        self.bot = bot


def setup(bot):
    bot.add_cog(Rumors(bot))
