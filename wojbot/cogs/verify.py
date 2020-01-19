from discord.ext import commands

DEFAULT_ROLES = [
    'mods',
    'Champion',
    'Commish',
]


class Verify(commands.Cog, name='Verify'):
    """
    This is a cog for use in other cogs. It verifies that the person running the command has authority to, based on
    whatever structure the league owner wants.

    TODO: find a way to give an option for commissioner.
    """
    def __init__(self, bot):
        self.bot = bot
        self.roles = bot.higherups + DEFAULT_ROLES

    async def verify(self, member):
        roles = member.roles
        verified = 0
        for role in roles:
            verified += role.name in self.roles
        return bool(verified)

    async def commissioner(self, member):
        roles = member.roles
        verified = 0
        for role in roles:
            verified += role.name in ['Commish']
        return bool(verified)


def setup(bot):
    bot.add_cog(Verify(bot))
