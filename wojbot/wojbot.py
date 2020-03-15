from collections import defaultdict
import discord
from discord.ext import commands
import logging
import os
import json
import traceback
import pandas as pd


DESCRIPTION = """"""
TOKEN = os.environ['DISCORD_TOKEN']
FLAG = '$'
EXTENSIONS = [
    'cogs.messaging',
    'cogs.verify',
    'cogs.members',
    'cogs.commish',
    'cogs.players',
    'cogs.annuhlitucks',
    # 'cogs.rumors',
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

FLAG_MAP = {
    'week': {
        'w', 'week'
    },
    'load': {
        'load', 'l'
    },
    'override': {
        'o', 'override'
    },
    'silent': {
        's', 'silent'
    }
}


def startup(creation):
    for extension in EXTENSIONS:
        try:
            creation.load_extension(extension)
        except Exception as e:
            traceback.print_exc()


class WojBot(commands.Bot):
    """
    This is WojBot, hear him roar! He is and forever will be my masterpiece.
    """

    logger = logging.getLogger(__file__)

    creds = None

    """
    This is for you, as a user, to edit. Depending on what roles you have in your league and what permissions you want 
    to give them, just put the role names for those users you want to have special bot permissions in here.
    """
    higherups = [

    ]

    """
    These are additional command flags. All cogs load from this flag map, and then adjust their own from there. I am not
    sure if the discord.py bot has it's own functionality for this, but this was interesting to build functionality
    around. I wonder how it actually works.
    """
    flag_map = FLAG_MAP

    """
    Can't think of a better name lol
    """
    data_model = {

    }

    """
    This is the memory management system for the bot. While the data model above should be persistent, these are the
    tools and files that the bot doesn't need to stay active, but may need for specific tasks. Each of these can be
    emptied automatically, or by command through the Garbage Collection module.
    """
    garbage = dict()

    recycling = dict()
    rc = False

    compost = {
        'drafts': defaultdict(list)
    }
    cc = False

    def __init__(self, path='resources', mode='.csv', creds=None):
        """
        Loads up WojBot
        :param path: str. Directory to load files from.
        :param mode: str. Whether to load from .csv files, or using .sql files
        :param creds: str, dict. Credentials to be passed to the various tools. Tools have their own ways to look up
                                 credentials from environment variables. Sending creds through here will override those.

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
        """
        Creates a credential dictionary based on input
        :param creds: str, dict. If a str, treats it as a file path (relative to cwd), and loads a json. If that doesn't
                                 work, treats it as a json blob.
        :return: dict. Credentials.
        """
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

    def _load_csv(self):
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
        """
        Part of start up. Loads data uesd by tools and bot. Currently both modes are functionally the same.
        """
        if self.mode == '.csv':
            self._load_csv()
        elif self.mode == '.sql':
            self._load_sql()

    def _dump_pd(self):
        for key, value in self.data_model.items():
            value.to_csv(os.path.join(self.path, key + self.mode))

    def dump_data_model(self):
        """
        Writes out data model to either local CSVs or to a SQL database
        """
        if self.mode == '.csv':
            self._dump_pd()
        elif self.mode == '.sql':
            pass

    async def on_ready(self):
        """
        Personal imp of on_ready
        """
        """http://discordpy.readthedocs.io/en/rewrite/api.html#discord.on_ready"""

        self.logger.info(f'Logged in as: {self.user.name} - {self.user.id}\nVersion: {discord.__version__}')

        # Need to come up with something better for this
        # await self.change_presence(activity=discord.Streaming(name='Cogs Example', url='https://twitch.tv/kraken'))
        self.logger.info(f'Successfully logged in and booted...!')

    def run(self):
        super().run(TOKEN, reconnect=True, bot=True)


if __name__ == '__main__':
    bot = WojBot()
    startup(bot)
    bot.run()
