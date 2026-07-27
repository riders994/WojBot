from discord.ext import commands
import random

DAD_TOGGLES = {'dad', 'dadjoke', 'dad joke', 'im'}


class Messaging(commands.Cog, name='Messaging Commands'):
    """
    These are the commands associated with sending and receiving messages at a basic level.
    """
    def __init__(self, bot):
        self.bot = bot
        self._last_member = None
        self.load = bot.path

    @commands.command()
    async def toggle(self, ctx, *, toggle: str):
        """
        Toggles certain message types. For Dad Jokes off, use 'dad' or 'dad joke'
        """
        if toggle in DAD_TOGGLES:
            old = self.bot.dadjoke_toggle[ctx.channel.id]
            self.bot.dadjoke_toggle[ctx.channel.id] = not old
            await ctx.send('Dad jokes toggled to {} for this channel.'.format(old))

    @commands.Cog.listener()
    async def on_message(self, message):
        """
        Dadjoke detection lies within
        :param message: Scans the message for dad-jokable material
        :return: Sends a message
        """
        if self.bot.dadjoke_toggle.get(message.channel.id):
            pass
        else:
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
        """
        Basic echo function. Replies with the message after the command. Or not.
        """
        if len(args):
            await ctx.send(' '.join(args))
        else:
            await ctx.send('SHUT UP')

    @commands.command()
    async def cat(self, ctx):
        """
        Sends a cat gif. Eventually there will be more.
        """
        try:
            if self.load:
                with open(self.load + 'cat_gifs.txt', 'rb') as file:
                    cats = set(file.readlines())
            else:
                cats = set()
        except FileNotFoundError:
            cats = set()
        cats.update({"https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif"})
        await ctx.send(random.choice(list(cats)))

    @commands.command()
    async def add(self, ctx, *, addends: str):
        """
        Adds some fuckin numbers together you idiot.
        """
        s = 0
        addends = addends.replace(' ', '').split(',')
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
