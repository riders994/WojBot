import pandas as pd
from discord.ext import commands
from .commonfuncs import create_flag_dict
from yahooelosystem import YahooEloSystem


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
        self.path = bot.path
        self.verifier = self.bot.get_cog('Verify')
        self.loader = self.bot.get_cog('Commissioner Tools')
        self.map = bot.flag_map

    def _setup_elos(self, flag_dict, ctx, week, last_week):
        if not self.loaded:
            self.loader.load_league(ctx, self.last_league)
            self.league_info = self.bot.compost['league_info']
            self.loaded = True

        for check in self.map['override']:
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
    async def run_elos(self, ctx, *, specs: str = None):
        """
        Loads a dataframe of Elos, or runs a scraper to get ratings.
        """
        if specs is None:
            await ctx.send('This feature is not yet available.')
        else:
            if self.verifier is not None:
                permed = await self.verifier.verify(ctx)
                if permed:
                    flag_dict = create_flag_dict(specs)
                    for check in self.map['week']:
                        week = flag_dict.get(check)
                        if week:
                            week = week[0]
                            break
                    for check in self.map['silent']:
                        silent = flag_dict.get(check)
                        if silent:
                            silent = silent[0]
                            break
                    if week.find(':') + 1:
                        last_week = int(week.split(':')[-1])
                    else:
                        last_week = int(week)
                    ovr = self._setup_elos(flag_dict, ctx, week, last_week)
                    self.yes.run(ovr)
                    df = self.yes.data_model['weekly_elos']

                    if not silent:
                        await ctx.send(f"Here are the Elo ratings for week {last_week}:")
                    ids = df.index
                    last_week = str(last_week)
                    try:
                        elos = df['week_' + last_week]
                        players = self.bot.data_model['players']
                        teams = self.bot.data_model['teams']
                        if not silent:
                            for player in ids:
                                name = players.loc[player]['name']
                                await ctx.send("{} has an Elo of {}".format(name, round(elos.loc[player], 2)))
                        self.yes.dump()
                    except ValueError:
                        await ctx.send("Well that shit didn't work")

                else:
                    await ctx.send('Verification failed, please try again')


def setup(bot):
    bot.add_cog(Analytics(bot))
