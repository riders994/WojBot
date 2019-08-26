from collections import defaultdict


def instructions(path='./'):
    with open(path + 'instructions.txt', 'r+') as file:
        return file.read()


def parse_message(message):
    sep = message.split(' ')
    command = []
    check = ''
    i = 1
    while check != '-':
        command.append(sep[i])
        check = sep[i][0]
        i += 1
    return {'_'.join(command): parse_flags(sep[i:])}


def parse_flags(message):
    spot = -1
    flag = ''
    res_dict = dict()
    for i, arg in enumerate(message):
        if arg[0] == '-':
            flag = arg.strip('-')
            if not res_dict.get(flag):
                # I'm doing this so that it catches flags even if they have no params.
                res_dict.update({flag: []})
        else:
            res_dict[flag].append(arg)
    for key, item in res_dict.items():
        res_dict.update({key: ' '.join(item)})
    return res_dict


def tests():
    print(instructions())


if __name__ == '__main__':
    tests()

