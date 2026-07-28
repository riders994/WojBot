import os


DESCRIPTION = """"""
TOKEN = os.environ['DISCORD_TOKEN']
FLAG = '$'
EXTENSIONS = [
    # 'cogs.messaging',
    # 'cogs.dice',
    # 'cogs.verify',
    # 'cogs.members',
    # 'cogs.commish',
    # 'cogs.players',
    # 'cogs.annuhlitucks',
    'cogs.rumors',
    'cogs.bot_setup'
]

PARAMS = {
    'consumer_key': os.environ.get('CONSUMER_KEY'),
    'consumer_secret': os.environ.get('CONSUMER_SECRET'),
    'token_time': os.environ.get('TOKEN_TIME'),
    'token_type': os.environ.get('TOKEN_TYPE'),
    'refresh_token': os.environ.get('REFRESH_TOKEN')
}

BOT_SECRET_LIST = [

]

YAHOO_SECRET_LIST = [

]

SLEEPER_SECRET_LIST = [

]

FANTRAX_SECRET_LIST = [

]

MODES = {'csv', 'sql'}

DEFAULT_FLAGS = {
    'week': {
        'w', 'week'
    },
    'override': {
        'o', 'override'
    },
    'silent': {
        's', 'silent'
    }
}

SETUP_FLAGS = {
    'bot': {
        'b', 'bot'
    },
    'league': {
        'l', 'league'
    },
    'server': {
        's', 'server'
    },
}

RUMOR_FORM_QUERIES = {
    'write_fact_rumor',
    'read_dim_rumor_form',
    'read_dim_release_type',
    'read_dim_source_type',
    'read_fact_rumor',
}

ELO_QUERIES = {
    'get_last_week',
    'write_next_week',
}

LEAGUE_INFO_QUERIES = {
    'read_dim_team',
    'read_dim_manager',
    'read_dim_league',
    'read_fact_league_managers',
    'read_fact_elos'
}

UPDATE_QUERIES = {
    'update_manager'
    'upsert_team_new_name'
    'upsert_team_finale'
}

INSERT_QUERIES = {
    'insert_new_league',
    'insert_new_team',
    'insert_new_manager',
}

DEFAULT_BOT_CONFIG = dict()

DEFAULT_LEAGUE_CONFIG = {
    'dynasty': False,
}

DEFAULT_SERVER_CONFIG = {
    'dad_joke': False,
}