import pandas as pd
from discord.ext import commands
from .commonfuncs import create_flag_dict
# from yahooelosystem import YahooEloSystem

FLAG_MAP = {
    'week': {
        'w', 'week'
    },
    'load': {
        'load', 'l'
    }
}


class Analytics(commands.Cog, name="Analytics"):
    """
    This Cog will run my league analytics. Can't wait to expand with the league, player, and email cogs later
    """
    def __init__(self, bot):
        self.bot = bot
        self.info = bot.league_info
        self.load = bot.load

    @commands.command()
    async def run_elos(self, ctx, *, flags: str):
        """
        Loads a dataframe of Elos, or runs a Selenium scraper to get ratings.
        """
        await ctx.send('verifying...')
        verifier = self.bot.get_cog('Verify')
        if verifier is not None:
            permed = await verifier.verify(ctx.author)
            if permed:
                flag_dict = create_flag_dict(flags)
                df = None
                for check in FLAG_MAP['load']:
                    load = flag_dict.get(check)
                    if load:
                        if self.load:
                            df = pd.read_csv(self.load + '/weekly_elos.csv')
                        else:
                            await ctx.send('Cannot load Elo frame, running scraper')
                for check in FLAG_MAP['week']:
                    week = flag_dict.get(check)
                    if week:
                        week = week[0]
                        break
                await ctx.send(week)

                if not df:
                    pass
                    league_server = ctx.guild.id
                    # runner = YahooEloSystem(league=self.info[league_server]['league'], week=week,
                    #                         players=self.info[league_server]['players'])
                    # runner.run()
                    # df = runner.elo_calc.weekly_frame

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
