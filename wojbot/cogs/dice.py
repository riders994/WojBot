from discord.ext import commands
from .commonfuncs import parse_dice


class Roller(commands.Cog, name='Dice'):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def roll(self, ctx, *, msg:str):
        await ctx.send(parse_dice(msg))


def setup(bot):
    bot.add_cog(Roller(bot))