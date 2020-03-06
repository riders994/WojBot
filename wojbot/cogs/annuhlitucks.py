import pandas as pd
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


class Analytics(commands.Cog, name="Analytics"):

    last_league = None
    league_info = None
    loaded = False
    yes = None

    """
    This Cog will run my league analytics. Can't wait to expand with the league, player, and email cogs later
    """
    def __init__(self, bot):
        self.bot = bot
        self.info = bot.league_info
        self.path = bot.path
        self.verifier = self.bot.get_cog('Verify')
        self.loader = self.bot.get_cog('Load')

    def _setup_elos(self, flag_dict, ctx):

        if not self.loaded:
            self.loader.load_league(ctx, self.last_league)
        for check in FLAG_MAP['week']:
            week = flag_dict.get(check)
            if week:
                week = week[0]
                break
        for check in FLAG_MAP['override']:
            ovr = flag_dict.get(check)
            if ovr:
                ovr = ovr[0]
                break

        if not self.yes:
            self.yes = YahooEloSystem(
                self.league_info, week, self.bot.recycling.get('scraper'), self.bot.creds,
                self.bot.mode, self.bot.path.replace('resources', '')
            )
            self.bot.recycling.update({'elo_system': self.yes})
            self.bot.rc = True
        return ovr

    @commands.command()
    async def run_elos(self, ctx, *, flags: str):
        """
        Loads a dataframe of Elos, or runs a scraper to get ratings.
        """
        await ctx.send('verifying...')

        if self.verifier is not None:
            permed = await self.verifier.verify(ctx.author)
            if permed:
                flag_dict = create_flag_dict(flags)
                df = None
                for check in FLAG_MAP['load']:
                    load = flag_dict.get(check)
                    if load:
                        if self.path:
                            df = pd.read_csv(self.path + '/weekly_elos.csv')
                        else:
                            await ctx.send('Cannot load Elo frame, running scraper')
                for check in FLAG_MAP['week']:
                    week = flag_dict.get(check)
                    if week:
                        week = week[0]
                        break
                await ctx.send(week)

                if not df:
                    ovr = self._setup_elos(flag_dict, ctx)
                    self.yes.run(ovr)
                    df = self.yes.elo_calc.weekly_frame

                await ctx.send(f"Here are the Elo ratings for week {week}.")
                names = df.index
                try:
                    elos = df['week_' + week]
                    for name in names:
                        await ctx.send("{} has an Elo of {}".format(name, round(elos[name], 2)))
                except ValueError:
                    ctx.send("Well that shit didn't work")

            else:
                await ctx.send('Verification failed, please try again')


def setup(bot):
    bot.add_cog(Analytics(bot))
