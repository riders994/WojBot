from discord.ext import commands
import random


class Messaging(commands.Cog, name='Messaging Commands'):
    def __init__(self, bot):
        self.bot = bot
        self._last_member = None
        self.load = bot.load

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.id == self.bot.user.id:
            return
        rcv = message.content
        dad = rcv.find('I am') + 1
        if not dad:
            dad = rcv.find('i am') + 1
        if dad:
            dad += 1
        if not dad:
            dad = rcv.find("I'm") + 1
        if not dad:
            dad = rcv.find("i'm") + 1
        if not dad:
            dad = rcv.find('I’m') + 1
        if not dad:
            dad = rcv.find('i’m') + 1
        if dad:
            dad += 3
            msg = "Hi {}, I'm dad".format(message.content[dad:].capitalize())
            await message.channel.send(msg)

    @commands.command()
    async def echo(self, ctx, *args):
        if len(args):
            await ctx.send(' '.join(args))
        else:
            await ctx.send('SHUT UP')

    @commands.command()
    async def cat(self, ctx):
        if self.load:
            with open(self.load, 'rb') as file:
                cats = set(file.readlines())
        else:
            cats = set()
        cats.update({"https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif"})
        await ctx.send(random.choice(list(cats)))

    @commands.command()
    async def add(self, ctx, *, addends: str):
        s = 0
        addends = addends.replace(',', '').split(' ')
        for a in addends:
            try:
                try:
                    s += int(a)
                except ValueError:
                    s += float(a)
            except ValueError:
                await ctx.send("{} isn't a number, dumbass".format(a))
        await ctx.send(s)


def setup(bot):
    bot.add_cog(Messaging(bot))
