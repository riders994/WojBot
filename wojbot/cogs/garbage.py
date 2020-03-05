from discord.ext import commands


class Garbage(commands.Cog, name='Memory Management Commands'):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def garbage_collect(self):
        self.bot.garbage = dict()

    @commands.command()
    async def recycling_collect(self):
        if self.bot.rc:
            self.bot.recycling = dict()
            self.bot.rc = False
        await self.garbage_collect()

    @commands.command()
    async def compost_collect(self):
        if self.bot.cc:
            self.bot.compost = dict()
            self.bot.cc = False
        await self.recycling_collect()


def setup(bot):
    bot.add_cog(Garbage(bot))
