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
    'league_choices': None,
    'choice': None,
}

STAGE_MAP = [
    'league', 'choice', 'form', 'source', 'release'
]

SOURCE_MESSAGE = """
                 ({Index}): {source_name}, a level {source_level} source (1-5 scale). 
                 If this is a 1, other teams will know this is from the team that it is about, or a verified internal source: {internal}.
                 """


class Rumors(commands.Cog, name='Rumor Commands'):

    madlib_keys = MADLIBS
    drafts = defaultdict(dict)
    progress = defaultdict(int)
    current = dict()
    stages = STAGE_MAP
    league_choices = dict()
    viz = defaultdict(lambda: 1)
    timeout = defaultdict(lambda: 5)

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.sources = bot.data_model['source_types']
        self.rumor_forms = bot.data_model['rumor_forms']
        self.rumors = bot.data_model['rumors']
        self.releases = bot.data_model['release_types']

    async def rumor_guide(self, ctx):
        pass

    def _set_timeout(self, ctx, t=5.0):
        self.timeout.update({ctx.author.id: t})

    async def set_timeout(self, ctx, *, time: float):
        self._set_timeout(ctx, time)

    async def reset_timeout(self, ctx):
        self._set_timeout(ctx)

    def _load(self):
        self.drafts.update(self.bot.recycling['drafts'])
        self.timeout.update(self.bot.compost['timeouts'])

    def _dump(self):
        self.bot.recycling.update(self.drafts)
        self.bot.rc = True
        self.bot.compost.update({'timeouts': self.timeout})
        self.bot.cc = True

    def _initial_build(self, ctx, flag_dict, draft):
        res = DRAFT_MODEL.copy()
        playerid = draft['player']

        for key in res.keys():
            res.update({key: flag_dict.get(key)})

        for mad, lib in MADLIBS.items():
            fill = flag_dict.get(mad)
            if fill:
                res.update({lib: fill[0]})
        league_list = self._get_leagues(playerid=playerid)
        self.progress[playerid] = self._get_stage(res)
        if len(league_list) - 1:
            league = league_list[0]
        else:
            league = self._league_prompt(playerid, ctx)
        self.current.update({playerid: res})
        self.drafts[playerid].update({league: res})

    def _map_leagues(self):
        pass

    def _get_leagues(self, playerid=None, leagueid=None):
        if playerid:
            players = self.bot.data_model['players']
            player = players.reset_index('discord_id')['playerid'].loc[playerid]
            teams = self.bot.data_model['teams']
            return list(teams[(teams['discordid'] == player) & teams['current']]['league'])
        if leagueid:
            pass

    def _build_draft(self, draft):
        pass
        # return draft

    async def _league_prompt(self, player, message):
        draft = self.current[player]
        rcv = message.content
        try:
            rcv = int(rcv)
        except ValueError:
            pass
        league = draft['league_choices'][rcv]
        self.drafts[player].update({league: draft})
        self.progress[player] = self._check_stage(player)
        self.current[player] = draft
        await self._ingest_response(self.timeout[player])

    async def _choice_prompt(self, player, message):
        draft = self.current[player]
        rcv = message.content
        try:
            rcv = int(rcv)
        except ValueError:
            pass
        draft.update({'choice': rcv})
        self.progress[player] += rcv
        rcv -= 1
        if rcv:
            viz = self.viz.get('')
            await message.author.send('')
            await message.author.send('')
            await self._give_sources(message.author)
        else:
            pass

    async def _form_prompt(self):
        pass

    async def _give_sources(self, recipient, waits=0):
        draft = self.current[recipient.id]
        did_form = draft.get('form')
        if did_form is None:
            for row in self.sources.itertuples():
                drow = row._asdict()
                await recipient.send(SOURCE_MESSAGE.format(drow))
            await self._ingest_response(self.timeout[recipient.id], 4)

    async def _source_prompt(self, player, message):
        rcv = message.content

    async def _release_prompt(self):
        pass

    @staticmethod
    def _get_stage(model):
        for i, stage in enumerate(STAGE_MAP):
            if not model.get(stage):
                return i
        return -1

    def _check_stage(self, player):
        return self._get_stage(self.current[player])

    async def _do_stage(self, stage, player, message):
        process = STAGE_MAP[stage]
        prompt = getattr(self, '_' + process + '_prompt')
        await prompt(player, message)

    async def start_rumor(self, ctx, *, specs: str = None):
        draft = DRAFT_MODEL.copy()
        auth = ctx.author
        discid = auth.id
        draft.update({'player': discid})
        n = ctx.channel.type.name
        if n == 'text':
            await ctx.send("Let's take this to private chat. Wouldn't want the whole league to know everything. ;)")
        else:
            await ctx.send("Oooh a new juicy rumor. I can't wait! Let's get this one ready for publication.")
        if specs is None:
            await ctx.send("Alright, looks like we're starting from scratch. Let's get started!")
            leagues = self._get_leagues(playerid=ctx.author.id)
            if len(leagues) - 1:
                ctx.author.send("Looks like you're in the following leagues:")
                draft.update({'league_choices': leagues})
                for i, league in enumerate(leagues):
                    # TODO: Fix to look up league name
                    ctx.author.send('{}: {}'.format(i, league))
                ctx.author.send("Pick a league")
            else:
                league = leagues[0]
                draft.update({'league': league})
                self.progress[discid] += 1
                ctx.author.send("Looks like you're in only one league, {league}. Would you like to (1) choose a form, "
                                "or (2) choose a source?".format(league=league))
                self.drafts[discid].update({league: draft})
            self.current[discid] = draft
            await self._ingest_response(self.timeout[discid])

        else:
            await ctx.send("Alright, let's finish setting this rumor up.")
            flag_dict = create_flag_dict(specs)

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

    async def _ingest_response(self, to=5, stageor=None):
        msg = await self.bot.wait_for('message', timeout=to)
        discid = msg.author.id
        if discid not in self.drafts.keys():
            await self._ingest_response()
        else:
            if stageor:
                stage = stageor
            else:
                stage = self._check_stage(discid)
            await self._do_stage(stage, discid, msg)


def setup(bot):
    bot.add_cog(Rumors(bot))
