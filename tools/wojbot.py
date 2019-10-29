import os
import json
import time
import random
import logging
import asyncio
import numpy as np
from discord.ext import commands
from message_commands import cat, echo
from yahooelosystem import YahooEloSystem
from commonfuncs import create_flag_dict


LEAGUE = '12682'
PLAYER_INFO = './resources/players.json'

with open(PLAYER_INFO, "rb") as data:
    PLAYERS = json.load(data)

FLAG_MAP = {
    'week': {
        'w', 'week'
    }
}

_logger = logging.getLogger('woj_bot')


PATH = './resources/drafter_values_2019.json'
LOTTERIED = 6
PICK_NAMES = '1st,2nd,3rd,4th,5th,6th,7th,8th,9th,10th,11th,12th'.split(',')
ADMIN_NICK = os.environ['ADMIN_NICK']
ADMIN_ID = os.environ['ADMIN_ID']
DRAFTERS = 'a,b,c,d,e,f,g,h,i,j,k,l'.split(',')
ODDS = [116, 111, 105, 98, 92, 86, 80, 74, 68, 63, 57, 50]


@commands.command()
async def instructions(ctx, path='./resources/'):
    try:
        with open(path + 'instructions.txt', 'r+') as file:
            await ctx.send(file.read())
    except FileNotFoundError:
        await ctx.send('No instructions found. Sorry!')


@commands.command()
async def draft_lottery(ctx, *args, filepath=PATH):
    if len(args):
        if args[0] in {'test', '-t', 't'}:
            ball_pit = np.arange(1000)
            await ctx.send('Running test draft')
            np.random.shuffle(ball_pit)
            selected_players = set()
            pick = 1
            i = 0
            combos = np.array(sum([[DRAFTERS[j]] * ODDS[j] for j in range(len(ODDS))], []))

            while pick <= LOTTERIED:
                ball_drawn = ball_pit[i]
                i += 1
                player_drawn = combos[ball_drawn]
                if player_drawn not in selected_players:
                    selected_players.update(player_drawn)
                    await ctx.send(player_drawn + ' ' + str(pick))
                    pick += 1

            for player in DRAFTERS:
                if player not in selected_players:
                    selected_players.update(player)
                    await ctx.send(player + ' ' + str(pick))
                    pick += 1

        if args[0] == 'verify':
            await ctx.send('verifying...')
            if ctx.author.name == ADMIN_NICK and ctx.author.discriminator == ADMIN_ID:
                try:
                    with open(filepath, 'rb') as file:
                        players_dict = json.load(file)
                except FileNotFoundError:
                    raise

                taken_picks = {0}
                selected_players = []

                i = 0
                player_pick_dict = {}

                players = players_dict['players']
                placement = players_dict['placement'].split(',')

                combos = np.array(sum([[player] * odds_prefs[0] for player, odds_prefs in players.items()], []))
                balls = combos.size
                ball_pit = np.arange(balls)
                np.random.shuffle(ball_pit)
                np.random.shuffle(combos)

                # 5.0 Here's the meat and potatoes
                #     TODO: Make this cleaner, better, make it a class maybe?

                # 5.1 This way it ends when 6 picks have been lotteried, as the 7th one breaks
                while len(taken_picks) <= LOTTERIED:
                    # 5.2 Draws the next player and iterates
                    ball_drawn = ball_pit[i]
                    i += 1
                    player_drawn = combos[ball_drawn]

                    # 5.3 Checks if player has already been drawn
                    if player_drawn in selected_players:
                        await ctx.send('Player picked again!')
                        pass
                    else:
                        # 5.4.0 If a new player's name has been drawn, adds them
                        await ctx.send('{} player picked! I wonder who it is...'.format(PICK_NAMES[len(taken_picks) - 1]
                                                                                        ))
                        selected_players.append(player_drawn)
                        # 5.4.1 Checks their preferences to see which pick they actually get
                        pick_prefs = players[player_drawn][1]
                        current_pick = 0
                        j = 0
                        while current_pick in taken_picks:
                            current_pick = pick_prefs[j]
                            j += 1
                        # 5.5 Adds it to the record
                        taken_picks.update([current_pick])
                        player_pick_dict[PICK_NAMES[current_pick - 1]] = player_drawn
                # That's the end of the lottery

                # Just for fun, calculate the probability of the result. Rull easy.
                prob = 1
                for name in selected_players:
                    k = players[name][0]
                    player_prob = k/balls
                    prob *= player_prob
                    balls -= k
                await ctx.send('The probability of this result was {}%!'.format(round(prob * 100, 5)))

                # 6.0 This is a similar process, but for the non-lotteried picks. These are
                #     by playoff finish.
                for name in placement:
                    if name not in selected_players:
                        selected_players.append(name)
                        pick_prefs = players[name][1]
                        current_pick = 0
                        while current_pick in taken_picks:
                            current_pick = pick_prefs[0]
                            pick_prefs = pick_prefs[1:]
                        taken_picks.update([current_pick])
                        player_pick_dict[PICK_NAMES[current_pick - 1]] = name

                # 7.0 Finale time!
                #     This sends the final lottery results to the group, in order from last
                #     to first!

                # 7.1 I put my thang down flip it and reverse it
                PICK_NAMES.reverse()
                for i, name in enumerate(PICK_NAMES):
                    await ctx.send('The {} pick goes to...'.format(name))
                    # 7.2 sleeps added to build DRAMA
                    s1 = i/(i + 1)
                    time.sleep(s1)
                    await ctx.send('{}!'. format(player_pick_dict[name]))
                    if i:
                        time.sleep(s1 ** 0.5)
            else:
                await ctx.send('Verification failed, please try again')


@commands.command()
async def run_elos(ctx, *args):
    if len(args):
        if args[0] in {'test', '-t', 't'}:
            await ctx.send("There's no test for this yet, dumbass")
    if args[0] == 'verify':
        await ctx.send('verifying...')
        if ctx.author.name == ADMIN_NICK and ctx.author.discriminator == ADMIN_ID:
            flag_dict = create_flag_dict(args)
            for check in FLAG_MAP['week']:
                week = flag_dict.get(check)
                if week:
                    week = week[0]
                    break
            await ctx.send(week)
            runner = YahooEloSystem(league=LEAGUE, week=week, players=PLAYERS)
            runner.run()
            await ctx.send("Here are this week's Elo ratings so far.")
            df = runner.elo_calc.weekly_frame
            names = df.index
            elos = df['week_' + week]
            for name in names:
                await ctx.send("{} has an Elo of {}".format(name, round(elos[name], 2)))
        else:
            await ctx.send('Verification failed, please try again')

TOKEN = os.environ['DISCORD_TOKEN']
COMMANDS = [cat, echo, draft_lottery, run_elos]
FLAG = '$'


class WojBot(commands.Bot):
    flag = FLAG

    def __init__(self):
        super().__init__(command_prefix=FLAG)

    async def on_ready(self):
        print('Logged in as')
        print(self.user.name)
        print(self.user.id)
        print('------')

    async def on_message(self, message):
        if message.author.id == self.user.id:
            return
        rcv = message.content
        dad = rcv.find('I am') + 1
        if not dad:
            dad = rcv.find('i am') + 1
        if dad:
            dad += 1
        if not dad:
            dad = rcv.find("I'm") + 1
        if not dad:
            dad = rcv.find("i'm") + 1
        if dad:
            dad += 3
            msg = "Hi {}, I'm dad".format(message.content[dad:].capitalize())
            await message.channel.send(msg)
        if rcv.startswith(self.flag):
            print('command received')
            print(message)
            if rcv.startswith('$add'):
                addends = rcv.split(' ')[1:]
                s = 0
                for a in addends:
                    try:
                        s += int(a)
                    except ValueError:
                        await message.channel.send("{} isn't a number, dumbass".format(a))
                await message.channel.send(s)

            if message.content.startswith('$guess'):
                await message.channel.send('Guess a number between 1 and 10.')

                def is_correct(m):
                    return m.author == message.author and m.content.isdigit()

                answer = random.randint(1, 10)

                try:
                    guess = await self.wait_for('message', check=is_correct, timeout=5.0)
                except asyncio.TimeoutError:
                    return await message.channel.send('Sorry, you took too long it was {}.'.format(answer))

                if int(guess.content) == answer:
                    await message.channel.send('You are right!')
                else:
                    await message.channel.send('Oops. It is actually {}.'.format(answer))
        await self.process_commands(message)


if __name__ == '__main__':
    bot = WojBot()
    # bot = WojCmd()
    for cmd in COMMANDS:
        bot.add_command(cmd)
    bot.run(TOKEN)
