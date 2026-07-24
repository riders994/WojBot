from discord.ext import commands


class Rumors(commands.Cog, name='Rumor Commands'):

    engine = None
    form_names = {
        'rumors', 'sources', 'releases'
    }
    logged_in = False
    sql_creds = None

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _build_sql_url(creds):
        return ''

    def _log_in(self):
        self.engine = self.bot.engine
        if self.engine:
            self.logged_in = True
        else:
            pass

    def load_tables(self):
        if not self.logged_in:
            self._log_in()
        pass

    def _load_forms(self):
        pass



async def setup(bot):
    await bot.add_cog(Rumors(bot))
