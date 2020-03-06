import discord
from discord.ext import commands
import logging
import os
import json
import traceback
import pandas as pd

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
    # 'cogs.annuhlitucks',
    'cogs.players',
    'cogs.members',
    'cogs.league',
    'cogs.rumors',
]
PARAMS = {
    'consumer_key': os.environ.get('CONSUMER_KEY'),
    'consumer_secret': os.environ.get('CONSUMER_SECRET'),
    'token_time': os.environ.get('TOKEN_TIME'),
    'token_type': os.environ.get('TOKEN_TYPE'),
    'refresh_token': os.environ.get('REFRESH_TOKEN')
}

MODES = {'csv', 'sql'}
TABLES = ['weekly_elo', 'leagues', 'players', 'teams']


class WojBot(commands.Bot):
    """
    This is WojBot, hear him roar! He is and forever will be my masterpiece.
    """

    creds = None

    higherups = [

    ]

    data_model = {

    }

    garbage = dict()

    recycling = dict()
    rc = False

    compost = dict()
    cc = False

    def __init__(self, path='resources', mode='.csv', creds=None):
        """
        Loads up WojBot
        :param path: Directory to load files from.

        TODO: Convert all loads to use os package best practices
        """
        super().__init__(command_prefix=FLAG, description=DESCRIPTION)
        self.path = path
        self.mode = mode
        self._load_data_model()
        if creds:
            try:
                self.params = self._load_creds(creds)
            except ValueError:
                raise
        else:
            self.params = PARAMS

        # how the fuck do CLAs work?
        # if load:
        #     self.load = self.options['load']

    @staticmethod
    def _load_creds(creds):
        if isinstance(creds, str):
            try:
                with open(os.path.join(os.getcwd(), creds), 'rb') as file:
                    dcreds = json.load(file)
            except FileNotFoundError:
                dcreds = json.loads(creds)
        elif isinstance(creds, dict):
            dcreds = creds
        else:
            raise ValueError
        return dcreds

    def _load_pd(self):
        for table in TABLES:
            if table == 'weekly_stats':
                pass
            try:
                frame = pd.read_csv(os.path.join(self.path, table + self.mode), index_col=0)
                self.data_model.update({table: frame})
            except FileNotFoundError:
                pass
        self.loaded = True

    def _load_sql(self):
        for table in TABLES:
            if table == 'weekly_stats':
                pass
            frame = pd.read_csv(os.path.join(self.path, table + self.mode), index_col=0)
            self.data_model.update({table: frame})
        self.loaded = True

    def _load_data_model(self):
        if self.mode == '.csv':
            self._load_pd()
        elif self.mode == '.sql':
            self._load_sql()

    async def on_ready(self):
        """http://discordpy.readthedocs.io/en/rewrite/api.html#discord.on_ready"""

        logger.info(f'Logged in as: {self.user.name} - {self.user.id}\nVersion: {discord.__version__}')

        # Need to come up with something better for this
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
