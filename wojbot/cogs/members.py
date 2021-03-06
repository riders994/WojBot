import discord
from discord.ext import commands


class MembersCog(commands.Cog, name="Member Commands"):
    """
    Commands associated with members. These are meant to work on non-league servers.
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.guild_only()
    async def joined(self, ctx, *, member: discord.Member = None):
        """
        Says when a member joined.
        """

        if member is None:
            member = ctx.author

        await ctx.send(f'{member.display_name} joined on {member.joined_at}')

    @commands.command(name='top_role', aliases=['toprole'])
    @commands.guild_only()
    async def show_toprole(self, ctx, *, member: discord.Member = None):
        """
        Simple command which shows the members Top Role.
        """

        if member is None:
            member = ctx.author

        await ctx.send(f'The top role for {member.display_name} is {member.top_role.name}')

    @commands.command(name='roles', aliases=['show_roles', 'list_roles'])
    @commands.guild_only()
    async def show_roles(self, ctx, *, member: discord.Member = None):
        """
        Simple command which shows the members roles
        """

        if member is None:
            member = ctx.author
        roles = [r.name for r in member.roles if r.name != '@everyone']
        await ctx.send(f'The roles for {member.display_name} are ' + ', '.join(roles))

    @commands.command(name='perms', aliases=['perms_for', 'permissions'])
    @commands.guild_only()
    async def check_permissions(self, ctx, *, member: discord.Member = None):
        """
        A simple command which checks a members Guild Permissions.
        If member is not provided, the author will be checked.
        """

        if not member:
            member = ctx.author

        # Here we check if the value of each permission is True.
        perms = '\n'.join(perm for perm, value in member.guild_permissions if value)

        # And to make it look nice, we wrap it in an Embed.
        embed = discord.Embed(title='Permissions for:', description=ctx.guild.name, colour=member.colour)
        embed.set_author(icon_url=member.avatar_url, name=str(member))

        # \uFEFF is a Zero-Width Space, which basically allows us to have an empty field name.
        embed.add_field(name='\uFEFF', value=perms)

        await ctx.send(content=None, embed=embed)
        # Thanks to Gio for the Command.


def setup(bot):
    bot.add_cog(MembersCog(bot))
