from .tools.wojbot import WojBot
import os

TOKEN = os.environ['DISCORD_TOKEN']

"""
This script is supposed to be used to manage the WojBot script, but I'm not sure if it'll ever be necessary.
"""

def _main():
    bot = WojBot()
    bot.run(TOKEN)
