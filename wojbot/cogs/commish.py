from discord.ext import commands
from yahooscrapingtools import YahooScrapingTools


class League(commands.Cog, name='Commissioner Tools'):

    last_league = None
    league_info = None
    loaded = False
    yes = None

    def __init__(self, bot):
        self.bot = bot
        self.yes = bot.recycling.get('elo_system')
        self.verifier = self.bot.get_cog('Verify')
        self.map = bot.flag_map

    def load_league(self, ctx, league=False):
        if league:
            league_table = self.bot.data_model['leagues']
            league_info = league_table[league_table['channelid'] == ctx.guild.id].to_dict(orient='records')[0]
            teams = self.bot.data_model['teams']
            team_map = teams[(teams['league'] == league_info['leagueid']) & (teams['current'])][[
                'owner', 'team_key'
            ]].set_index('team_key')
            league_info.update({'team_map': team_map.to_dict()['owner']})
            self.league_info = league_info
            self.bot.compost.update({'league_info': league_info})
            self.bot.cc = True
            self.loaded = True
        else:
            self.league_info = self.bot.compost.get('league_info')
            if self.league_info:
                self.last_league = self.league_info.get('channelid')
                self.yes = self.bot.recycling.get('elo_system')
                self.loaded = True
            else:
                self.load_league(ctx, True)

    @commands.command()
    @commands.guild_only()
    async def ylogin(self, ctx):
        s = self.bot.recycling.get('scraper')
        if s:
            if not s.sc.token_is_valid():
                ctx.send('Token expired. Manual intervention required.')
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
        if self.verifier is not None:
            permed = await self.verifier.commissioner(ctx)
            if permed:
                await ctx.send('Hello Commissioner. Backing up data.')
                self.bot.dump_data_model()
                await ctx.send('Data successfully backed up.')
            else:
                await ctx.send('Verification failed, please try again')




def setup(bot):
    bot.add_cog(League(bot))
