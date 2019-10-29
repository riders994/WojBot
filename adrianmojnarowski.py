from .tools.wojbot import WojBot
import os

TOKEN = os.environ['DISCORD_TOKEN']


def _main():
    bot = WojBot()
    bot.run(TOKEN)
