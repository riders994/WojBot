from collections import defaultdict

"""
Common functions that could be used amongst different Cogs
"""


def create_flag_dict(messages):
    res = defaultdict(list)
    flagged = 0
    key = None
    for msg in messages:
        if msg[0] == '-':
            key = msg.replace('-', '')
            flagged = 1
        if msg[0] == '+':
            res[msg.replace('+', '')].append(True)
        elif flagged:
            res[key].append(msg)
    return res
