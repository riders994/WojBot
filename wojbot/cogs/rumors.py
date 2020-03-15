from discord.ext import commands
from collections import defaultdict
from .commonfuncs import create_flag_dict

MADLIBS = {

}

DRAFT_MODEL = {
    'formid': None,
    'source': None,
    'league': None,
    'release': None,
    'player': None,
    'form_fill': None,
}

STAGE_MAP = [
    'league', 'form', 'source', 'release'
]


class Rumors(commands.Cog, name='Rumor Commands'):

    madlib_keys = MADLIBS
    drafts = defaultdict(list)
    progress = defaultdict(int)
    current = dict()
    stages = STAGE_MAP
    league_choices = dict()

    def __init__(self, bot):
        self.bot = bot

    def _load(self):
        self.drafts.update(self.bot.recycling['drafts'])

    def _dump(self):
        self.bot.recycling.update(self.drafts)

    def _initial_build(self, ctx, flag_dict):
        res = DRAFT_MODEL.copy()
        auth = ctx.author
        discid = auth.id
        playerid = self.bot.data_model['players'].set_index('discordid')['playerid'].loc[discid]

        for key in res.keys():
            res.update({key: flag_dict.get(key)})

        for mad, lib in MADLIBS.items():
            fill = flag_dict.get(mad)
            if fill:
                res.update({lib: fill[0]})
        l = self._get_leagues(playerid=playerid)
        self.progress[playerid] = self._get_stage(res)
        if len(l) - 1:
            pass
        else:
            pass
        self.current.update({playerid: res})
        self.drafts[playerid].append(res)

    def _map_leagues(self):
        pass

    def _get_leagues(self, playerid=None, leagueid=None):
        teams = self.bot.data_model['teams']
        return list(teams[(teams['owner'] == playerid) & teams['current']]['league'])

    async def _league_prompt(self, ctx):
        pass

    async def _form_prompt(self):
        pass

    async def _source_prompt(self):
        pass

    async def _release_prompt(self):
        pass

    @staticmethod
    def _get_stage(model):
        for i, stage in enumerate(STAGE_MAP):
            if not model.get(stage):
                return i
        return -1

    def _check_stage(self, player):
        return self.progress[player]

    async def _do_stage(self, stage, player):
        process = STAGE_MAP[stage]
        prompt = getattr(self, '_' + process + '_prompt')
        await prompt(player)

    async def start_rumor(self, ctx, *, specs: str = None):
        n = ctx.channel.type.name
        if n == 'text':
            await ctx.send("Let's take this to private chat. Wouldn't want the whole league to know everything. ;)")
        else:
            await ctx.send("Oooh a new juicy rumor. I can't wait! Let's get this one ready for publication.")
        if specs is None:
            await ctx.send("Alright, looks like we're starting from scratch. Let's get started!")
            flag_dict = dict()
        else:
            await ctx.send("Alright, let's finish setting this rumor up.")
            flag_dict = create_flag_dict(specs)
        self._initial_build(ctx, flag_dict)

    def check_rumors(self):
        pass

    def check_credibility(self):
        pass

    def _check_drafts(self):
        pass

    def check_drafts(self):
        pass

    def resume_draft(self):
        pass

    def discard_draft(self):
        pass

    async def _ingest_response(self):
        msg = await self.bot.wait_for('message', timeout=5)
        discid = msg.author.id
        if discid not in self.drafts.keys():
            await self._ingest_response()
        else:
            stage = self._check_stage(discid)
            await self._do_stage(stage, discid)


def setup(bot):
    bot.add_cog(Rumors(bot))
