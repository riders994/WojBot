import pandas as pd
from discord.ext import commands
from .commonfuncs import create_flag_dict, get_flag
from yahooelosystem import YahooEloSystem

FLAG_OVERRIDES = {
    'silent': {
        's', 'silent', 'v', 'verbose'
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
        self.logger = bot.logger
        self.bot = bot
        self.path = bot.path
        self.verifier = self.bot.get_cog('Verify')
        self.loader = self.bot.get_cog('Commissioner Tools')
        self.map = bot.flag_map

    def _setup_elos(self, flag_dict, ctx, week, last_week):
        """
        Makes sure everything is set up to run the elo system for a specific league.
        :param flag_dict: dict. Dictionary containing run parameters.
        :param ctx: discord.ctx. Context object from discord module.
        :param week: int, str. Week(s) that Elos will be run for
        :param last_week: int. Last week that Elos will be run for
        :return: NoneType, bool. Whether or not to override current values and re-calculate.
        """
        self.logger.info('Setting up Elos for week %s', week)
        self.logger.info('Checking load status')
        if not self.loaded:
            self.logger.info('No league loaded, checking last league')
            self.loader.load_league(ctx, self.last_league)
            self.logger.info('Getting league information')
            self.league_info = self.bot.compost.get('league_info')
            self.loaded = True

        self.logger.info('Checking for override')
        for check in self.map['override']:
            ovr = flag_dict.get(check)
            if ovr:
                ovr = ovr[0]
                break

        self.logger.info('Checking for Elo System')
        if self.yes:
            self.logger.info('System exists.')
            self.logger.info('Checking latest week')
            if self.yes.week < last_week:
                self.logger.info('Updating week')
                new_week = str(self.yes.week) + ':' + str(last_week)
                self.yes.set_week(new_week)
        else:
            self.logger.info('No system found. Creating a new one.')
            self.yes = YahooEloSystem(
                self.league_info, week, self.bot.recycling.get('scraper'), self.bot.creds,
                self.bot.mode, self.bot.path.replace('resources', '')
            )
            self.logger.info('Elo system successfully created. Updating recycling.')
            self.bot.recycling.update({'elo_system': self.yes, 'scraper': self.yes.scraper})
            self.bot.rc = True
        self.logger.info('Returning override')
        return ovr

    @commands.command()
    async def run_elos(self, ctx, *, specs: str = None):
        """
        Loads a dataframe of Elos, or runs a scraper to get ratings.
        """
        if specs is None:
            self.logger.info('Getting Elos for current week')
            await ctx.send('This feature is not yet available.')
        else:
            if self.verifier is not None:
                permed = await self.verifier.verify(ctx)
                if permed:
                    self.logger.info('User confirmed. Proceeding.')
                    flag_dict = create_flag_dict(specs)
                    self.logger.info('Checking week')
                    for check in self.map['week']:
                        week = flag_dict.get(check)
                        if week:
                            week = week[0]
                            break
                    self.logger.info('Checking for verbose')
                    for check in self.map['silent']:
                        silent = flag_dict.get(check)
                        if silent:
                            self.logger.info('Silencing')
                            silent = silent[0]
                            break
                    self.logger.info('Multi week?')
                    if week.find(':') + 1:
                        last_week = int(week.split(':')[-1])
                    else:
                        last_week = int(week)
                    ovr = self._setup_elos(flag_dict, ctx, week, last_week)
                    self.logger.info('Running Elos')
                    self.yes.run(ovr)
                    self.logger.info('Loading DF')
                    df = self.yes.data_model['weekly_elos']

                    if not silent:
                        await ctx.send("Here are the Elo ratings for week {last_week}:".format(last_week=last_week))
                    ids = df.index
                    last_week = str(last_week)
                    self.logger.info('Confirming week')
                    try:
                        elos = df['week_' + last_week]
                        players = self.bot.data_model['players']
                        teams = self.bot.data_model['teams']
                        self.logger.info('Elos have loaded.')
                        if not silent:
                            self.logger.info('Messaging to league.')
                            for player in ids:
                                name = players.loc[player]['name']
                                await ctx.send("{} has an Elo of {}".format(name, round(elos.loc[player], 2)))
                        self.logger.info('Ayy, it worked. Dumping results for posteriority.')
                        self.yes.dump()
                    except ValueError:
                        await ctx.send("Well that shit didn't work")
                    except KeyError:
                        self.logger.info('Past weeks have not been calculated yet')

                else:
                    self.logger.info('They a bitch')
                    await ctx.send('Verification failed, please try again')


def setup(bot):
    bot.add_cog(Analytics(bot))
