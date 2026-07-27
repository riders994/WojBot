from ..tools.basics.constants import DEFAULT_BOT_CONFIG, DEFAULT_LEAGUE_CONFIG, DEFAULT_SERVER_CONFIG

from discord.ext import commands
from typing import Dict, Any

import yaml
import os
import logging


class BotSetup(commands.Cog, name='Rumor Commands'):

    valid_leagues = set()
    valid_servers = set()

    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.config_loc = os.path.join(self.bot.path, 'configs')

    @commands.command()
    async def validate(self, ctx):
        pass

    @commands.command()
    async def status(self, ctx, *args):
        return self.bot.config is None

    @commands.command()
    async def setup(self, ctx, *args):
        if len(args):
            pass
        else:
            if os.path.isdir(self.config_loc):
                pass
            else:
                self.bot.logger.log(logging.WARN, '')
        pass

    def _setup_bot(self):
        if self.config is None:
            self.config = DEFAULT_BOT_CONFIG.copy()
        self.freeze()

    def _setup_server(self, id, user_choices):
        server_config = DEFAULT_SERVER_CONFIG.copy()
        server_config.update(user_choices)
        self.valid_servers.add(id)
        self.bot.logger.log(logging.INFO, 'Setting up server {}...'.format(id))
        self.bot.server_configs[id] = server_config

    def _setup_league(self, bot):
        pass

    def _freeze_multi(self, loc: str, vals: Dict[str, Any]):
        name = f'{loc}_configs'
        if not os.path.isdir(os.path.join(self.config_loc, name)):
            os.mkdir(os.path.join(self.config_loc, name))
        for k, v in vals.items():
            with open(os.path.join(self.config_loc, name, f'{k}.yaml'), 'r') as f:
                yaml.dump(v, f)

    def freeze(self):
        if not self.bot.config:
            self.bot.logger.log(logging.WARN, '')
            os.mkdir(self.config_loc)
        with open(os.path.join(self.config_loc, 'bot_config.yaml'), 'w') as f:
            yaml.dump(self.config, f)
        if len(self.valid_leagues):
            self._freeze_multi('league', self.bot.league_configs)
        if len(self.valid_servers):
            self._freeze_multi('servers', self.bot.server_configs)



async def setup(bot):
    await bot.add_cog(BotSetup(bot))
