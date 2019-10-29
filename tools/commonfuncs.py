from collections import defaultdict


def create_flag_dict(messages):
    res = defaultdict(list)
    flagged = 0
    key = None
    for msg in messages:
        if msg[0] == '-':
            key = msg.replace('-', '')
            flagged = 1
        elif flagged:
            res[key].append(msg)
    return res
