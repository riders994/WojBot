from collections import defaultdict

"""
Common functions that could be used amongst different Cogs
"""


def create_flag_dict(messages):
    res = defaultdict(list)
    is_flagged = None
    for i, msg in enumerate(messages.split(' ')):
        if msg[0] == '-':
            flag = msg.strip('-')
            is_flagged = i + 1
        if i == is_flagged:
            res[flag].append(msg)
        if msg[0] == '+':
            res[msg.replace('+', '')].append(True)

    return res
