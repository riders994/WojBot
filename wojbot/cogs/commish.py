from discord.ext import commands
from yahooscrapingtools import YahooScrapingTools


class League(commands.Cog, name='Commissioner Tools'):
    """
    These are commissioner tools. They are mostly to manage the other non-discord tools that power the functionality of
    the bot.
    """

    last_league = None
    league_info = None
    loaded = False
    yes = None

    def __init__(self, bot):
        self.logger = bot.logger
        self.bot = bot
        self.yes = bot.recycling.get('elo_system')
        self.verifier = self.bot.get_cog('Verify')
        self.map = bot.flag_map

    def load_league(self, ctx, league=False):
        """
        Loads a league, and the information involved in a league necessary for running bot operations.
        :param ctx: discord.ctx. Discord context which gives info.
        :param league: bool. Whether or not there is a specific league to be loaded.
        """
        if league:
            self.logger.info('Detecting league. Fetching tables.')
            league_table = self.bot.data_model['leagues']
            self.logger.info('Creating league info.')
            league_info = league_table[league_table['channelid'] == ctx.guild.id].to_dict(orient='records')[0]
            teams = self.bot.data_model['teams']
            self.logger.info('Adding teams')
            team_map = teams[(teams['league'] == league_info['leagueid']) & (teams['current'])][[
                'owner', 'team_key'
            ]].set_index('team_key')
            league_info.update({'team_map': team_map.to_dict()['owner']})
            self.logger.info('Setting values in memory')
            self.league_info = league_info
            self.bot.compost.update({'league_info': league_info})
            self.bot.cc = True
            self.loaded = True
        else:
            self.logger.info("Checking for loaded league")
            self.league_info = self.bot.compost.get('league_info')
            if self.league_info:
                self.logger.info('Setting values in memory')
                self.last_league = self.league_info.get('channelid')
                self.yes = self.bot.recycling.get('elo_system')
                self.loaded = True
            else:
                self.logger.info('Loading new league')
                self.load_league(ctx, True)

    @commands.command()
    @commands.guild_only()
    async def ylogin(self, ctx):
        """
        Logs in to Yahoo!
        """
        s = self.bot.recycling.get('scraper')
        if s:
            if not s.sc.token_is_valid():
                await ctx.send('Token expired. Manual intervention required.')
            s.sc.refresh_access_token()
            await ctx.send('Token successfully refreshed')
        else:
            scraper = YahooScrapingTools(self.bot.creds)
            await ctx.send('Creating Yahoo! engine, logging in')
            scraper.login()
            self.bot.recycling.update({'scraper': scraper})
            self.bot.rc = True
            await ctx.send('Successfully logged in, and stored engine')

    @commands.command()
    @commands.guild_only()
    async def dump(self, ctx):
        """
        Backs up data
        """
        if self.verifier is not None:
            permed = await self.verifier.commissioner(ctx)
            if permed:
                await ctx.send('Hello Commissioner. Backing up data.')
                self.bot.dump_data_model()
                await ctx.send('Data successfully backed up.')
            else:
                await ctx.send('Verification failed, please try again')

    async def initiate_league(self, ctx):
        pass

    async def refresh_league(self, ctx):
        pass

def setup(bot):
    bot.add_cog(League(bot))
