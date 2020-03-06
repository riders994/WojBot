from discord.ext import commands
from .commonfuncs import create_flag_dict
from yahooelosystem import YahooEloSystem

FLAG_MAP = {
    'week': {
        'w', 'week'
    },
    'load': {
        'load', 'l'
    },
    'override': {
        'o'
    }
}


class League(commands.Cog, name='League Commands'):

    last_league = None
    league_info = None
    loaded = False
    yes = None

    def __init__(self, bot):
        self.bot = bot
        self.yes = bot.recycling.get('elo_system')

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

    def _setup_elos(self, flag_dict, ctx, week, last_week):
        if not self.loaded:
            self.load_league(ctx, self.last_league)

        for check in FLAG_MAP['override']:
            ovr = flag_dict.get(check)
            if ovr:
                ovr = ovr[0]
                break

        if self.yes:
            if self.yes.week < last_week:
                new_week = str(self.yes.week) + ':' + str(last_week)
                self.yes.set_week(new_week)
        else:
            self.yes = YahooEloSystem(
                self.league_info, week, self.bot.recycling.get('scraper'), self.bot.creds,
                self.bot.mode, self.bot.path.replace('resources', '')
            )
            self.bot.recycling.update({'elo_system': self.yes, 'scraper': self.yes.scraper})
            self.bot.rc = True
        return ovr

    @commands.command()
    @commands.guild_only()
    async def run_elos(self, ctx, *, specs: str):
        flag_dict = create_flag_dict(specs)
        for check in FLAG_MAP['week']:
            week = flag_dict.get(check)
            if week:
                week = week[0]
                break
        if week.find(':') + 1:
            last_week = int(week.split(':')[-1])
        else:
            last_week = int(week)
        ovr = self._setup_elos(flag_dict, ctx, week, last_week)
        self.yes.run(ovr)
        df = self.yes.data_model['weekly_elos']

        if week.find(':') + 1:
            last_week = week.split(':')[-1]
        await ctx.send(f"Here are the Elo ratings for week {week}.")
        ids = df.index
        last_week = str(last_week)
        try:
            elos = df['week_' + last_week]
            players = self.bot.data_model['players']
            teams = self.bot.data_model['teams']
            for player in ids:
                name = players.loc[player]['name']
                await ctx.send("{} has an Elo of {}".format(name, round(elos[player], 2)))
        except ValueError:
            await ctx.send("Well that shit didn't work")


def setup(bot):
    bot.add_cog(League(bot))
