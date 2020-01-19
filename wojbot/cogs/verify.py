from discord.ext import commands

DEFAULT_ROLES = [
    'mods',
    'Champion',
    'Commish',
]


class Verify(commands.Cog, name='Verify'):
    def __init__(self, bot):
        self.bot = bot
        self.roles = bot.higherups + DEFAULT_ROLES

    async def verify(self, member):
        roles = member.roles
        verified = 0
        for role in roles:
            verified += role.name in self.roles
        return bool(verified)

    async def admin(self, member):
        roles = member.roles
        verified = 0
        for role in roles:
            verified += role.name in ['Commish']
        return bool(verified)


def setup(bot):
    bot.add_cog(Verify(bot))
