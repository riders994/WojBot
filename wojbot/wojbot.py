import discord
from discord.ext import commands
import logging
import os
import json
import traceback

logger = logging.getLogger('discord')
logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
logger.addHandler(handler)

DESCRIPTION = """"""
TOKEN = os.environ['DISCORD_TOKEN']
FLAG = '$'
EXTENSIONS = [
    'cogs.messaging',
    'cogs.verify',
    'cogs.annuhlitucks',
    'cogs.players',
    'cogs.members',
    'cogs.league',
    'cogs.rumors',
]
PLAYER_INFO = './resources/players.json'

with open(PLAYER_INFO, "rb") as data:
    PLAYERS = json.load(data)

LEAGUE_INFO = {
    '140333786381418496': {
        'league': '12682',
        'players':  PLAYERS
    }
}


class WojBot(commands.Bot):
    higherups = [

    ]
    options = [
        (('-l', '--load'), dict(type=str, dest='load', default='./resources',
                                help="""
                                Sets a destination to load files from. This should be a home directory.
                                  
                                TODO: Include a guide on the file structure
                                TODO: Add SQL functionality
                                """))
    ]

    league_info = LEAGUE_INFO

    def __init__(self, load='./resources'):
        super().__init__(command_prefix=FLAG, description=DESCRIPTION)
        self.load = load

        # if load:
        #     self.load = self.options['load']

    async def on_ready(self):
        """http://discordpy.readthedocs.io/en/rewrite/api.html#discord.on_ready"""

        logger.info(f'\n\nLogged in as: {self.user.name} - {self.user.id}\nVersion: {discord.__version__}\n')

        # Changes our bots Playing Status. type=1(streaming) for a standard game you could remove type and url.
        # await self.change_presence(activity=discord.Streaming(name='Cogs Example', url='https://twitch.tv/kraken'))
        logger.info(f'Successfully logged in and booted...!')

    def run(self):
        super().run(TOKEN, reconnect=True, bot=True)


if __name__ == '__main__':
    bot = WojBot()
    for extension in EXTENSIONS:
        try:
            bot.load_extension(extension)
            logger.info('Extension %s successfully loaded', extension)
        except Exception as e:
            logger.info('Failed to load extension %s.', extension)
            traceback.print_exc()

    bot.run()
