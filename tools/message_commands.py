import random
from discord.ext import commands


@commands.command()
async def echo(ctx, *args):
    if len(args):
        await ctx.send(' '.join(args))
    else:
        await ctx.send('SHUT UP')


@commands.command()
async def cat(ctx):
    await ctx.send("https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif")

