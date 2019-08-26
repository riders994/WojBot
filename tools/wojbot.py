import json
import time
from fbchat import Client, log, models
import logging
import random
from WojBot.tools import message_commands
from collections import defaultdict

SECRET = 'test'

# default globals
SLEEP = 2
"""
self._respond(
"""
""",
    thread_id,
    thread_type
)
"""


def steep(message, thread_id, thread_type):
    pass


class Thread:
    id = None

class Player(Thread):
    def __init__(self, name):
        self.name = name

    def link_thread(self, thread_id):
        self.id = thread_id


class Admin(Player):
    def link_thread(self, thread_id):
        super().link_thread(thread_id)

class League:
    thread = None
    players_lkup = set()
    num_players = None
    conferences = None
    divisions = None
    admin = None
    players = dict()

    def __init__(self, name, admin):
        self.name = name
        self.admin = admin
        pass

    def set_thread(self, thread_id):
        if not self.thread:
            self.thread = thread_id

    def reset_thread(self, thread_id):
        self.thread = thread_id

    def add_player(self, name, admin=False):
        if name not in self.players_lkup:
            self.players_lkup.update(name)
            if admin:
                self.admin = Admin(name)
            else:
                self.players.update({name: Player(name)})

    def link_player(self, name, thread_id):
        self.players[name].link_thread(thread_id)

class WojBot(Client):
    secret = SECRET
    players = defaultdict(set)
    admins = dict()
    leagues = dict()
    authorized_leagues = dict()
    in_funnel_users = dict()

    def __init__(
            self,
            email,
            password,
            sleep=0.7,
            instructions_path='./',
            user_agent=None,
            max_tries=5,
            session_cookies=None,
            logging_level=logging.INFO,
    ):
        self.sleep = sleep
        self.instr = instructions_path
        super().__init__(
            email,
            password,
            user_agent,
            max_tries,
            session_cookies,
            logging_level,
        )

    def send(self, message, thread_id=None, thread_type=ThreadType.USER):
        time.sleep(self.sleep)
        super().send(message, thread_id, thread_type)

    def _cu_step_1(self, message, thread_id, thread_type):
        yes = message[0].lower()
        if yes == 'y':
            self._respond(
                """
                What's the secret password?
                """,
                thread_id,
                thread_type
            )
            self.admins.update({thread_id: []})

        else:
            self._respond(
                """
                Cool! Which league do you belong to?
                """,
                thread_id,
                thread_type
            )

    def _cu2_verify_league(self, name):
        res = False
        for league in self.leagues.keys():
            l = league['name']
            name_check = l.name
            res += (name_check == name)
        return res

    def _cu_step_2(self, message, thread_id, thread_type):
        if thread_id in self.admins.keys():
            if message == self.secret:
                self._respond(
                    """
                    Great! Let's get started. What's the name of this league?
                    """,
                    thread_id,
                    thread_type
                )
            else:
                self._respond(
                    """
                    https://www.youtube.com/watch?v=RfiQYRn7fBg
                    """,
                    thread_id,
                    thread_type
                )
                del self.in_funnel_users[thread_id]
        else:
            if self._cu2_verify_league(message):
                self.players[thread_id].update(message)
                self._respond(
                    """
                    Great! What's your name?
                    """,
                    thread_id,
                    thread_type
                )
            else:
                self._respond(
                    """
                    Looks like that league name isn't recognized. Check your spelling, or check with the commish!
                    """,
                    thread_id,
                    thread_type
                )
                self.in_funnel_users[thread_id] = 2

    def _cu_step_3(self, message, thread_id, thread_type):
        if thread_id in self.admins.keys():
            self.authorized_leagues[thread_id].append(message)
            self._create_league(message, thread_id)
            self._respond(
                """
                Congratulations! The league {} has been created. This is your admin id: {}
                """.format(message, thread_id),
                thread_id,
                thread_type
            )
        else:
            league = self.leagues[self.players[thread_id]]
            if message in league.player_lkup:
                league.link_player(message, thread_id)

    def _create_user(self, thread_id, thread_type, details, message):
        if thread_id in self.in_funnel_users.keys():
            step = self.in_funnel_users[thread_id]
            stepper = steep
            self.in_funnel_users[thread_id] = step + 1
            stepper(message, thread_id, thread_type)

        else:
            self.in_funnel_users.update({thread_id: 1})
            self._respond("""
                          It looks like you're a new user. Are you creating a new league?
                          
                          y/n?
                          """, thread_id, thread_type
                          )

    def _listen_user(self, author_id, message, thread_id, thread_type, details=None):
        response = ''
        if author_id in self.admins.keys():
            pass
        elif author_id in self.players.keys():
            pass
        else:
            self._create_user(thread_id, thread_type, details, message)

        return response

    def _create_league(self, name, admin):
        self.leagues[name] = League(name, admin)

    def _link_league(self, thread, details):
        name = details['name']
        admin = self.leagues[name].admin
        if thread not in self.admins[admin]:
            self.admins[admin].append(thread)
        if details.get('r'):
            self.leagues[name].reset_thread(thread)
        else:
            self.leagues[name].set_thread(thread)

    @staticmethod
    def _check_create(message):
        return message_commands.parse_message(message).keys()[0] in {'create', 'create_league', '!c'}

    @staticmethod
    def _check_link(message):
        return message_commands.parse_message(message).keys()[0] in {'link', 'link_league', '!l'}

    def _listen_league(self, author_id, message, thread_id, details):
        response = ''
        is_thread = 0
        for lname, league in self.leagues.items():
            is_thread += league.thread == thread_id

        if is_thread > 0:
            if author_id in self.authorized_leagues.keys():
                if self._check_link(message):
                    self._link_league(thread_id, details)
                response = 'Thank you for linking this league!'
            else:
                response = 'This league is not authorized yet'
        return response

    def _parse_incoming(self, author_id, message, thread_id, thread_type):
        response = ''
        woj_check = message.split(' ')[0]
        if woj_check == '!wojbot':
            command = message_commands.parse_message(message)
            if thread_type == 'USER':
                self._listen_user(author_id, message, thread_id, thread_type, command)
            elif thread_type == 'GROUP':
                response = self._listen_league(author_id, message, thread_id, command)
        else:
            if thread_type == 'USER':
                self._listen_user(author_id, message, thread_id, thread_type)
        return response

    def _respond(self, message, thread_id, thread_type):
        time.sleep(self.sleep * random.random())
        self.send(models.Message(text=message), thread_id=thread_id, thread_type=thread_type)

    def onMessage(self, author_id, message_object, thread_id, thread_type, **kwargs):
        time.sleep(self.sleep * random.random())
        self.markAsDelivered(thread_id, message_object.uid)
        print('boop')
        time.sleep(self.sleep * random.random())
        self.markAsRead(thread_id)

        print('poob')
        log.info("{} from {} in {}".format(message_object, thread_id, thread_type.name))
        print('poop')
        # If you're not the author, echo
        if author_id != self.uid and thread_id in self.recognized_threads:
            print('boob')
            message = self._parse_incoming(author_id, message_object.text, thread_id)
            if len(message):
                self._respond(message, thread_id, thread_type)


if __name__ == '__main__':
    with open('./creds/login.json', 'rb') as file:
        credentials = json.load(file)

    test = WojBot(credentials['uid'], credentials['pwd'], SLEEP)
