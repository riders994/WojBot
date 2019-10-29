import pandas as pd
import numpy as np
import json
import logging

PLAYER_INFO = './resources/players.json'
WEEK = 1
FULL_ROWS = ['fgpct', 'ftpct', 'threes', 'points', 'rebounds', 'assists', 'steals', 'blocks', 'turnovers', 'true_score',
             'week']


_logger = logging.getLogger(__name__)


def elo_calc(player_1, player_2, k=60):
    share_a = np.power(10, player_1[0]/400)
    share_b = np.power(10, player_2[0]/400)
    total = share_a + share_b

    expected_a = share_a/total
    expected_b = share_b/total

    return [player_1[0] + k * (player_1[1] - expected_a), player_2[0] + k * (player_2[1] - expected_b)]


class EloCalc:

    week = 0
    weekly_frame = None

    def __init__(self, week):
        _logger.info('Elo calculator started')
        if week:
            _logger.info('Calculating Elos for week {}'.format(week))
            self.week = week
            self._load()
            if not self._verify():
                _logger.info(
                    'Error verifying. Make sure you have data for the previous week. Current week is {}'.format(week)
                )
                raise ValueError
        else:
            self._generate()

    def _generate(self):
        _logger.info('Generating week 0 frame')
        with open(PLAYER_INFO, "rb") as data:
            players = json.load(data)
            _logger.info(players)

        _logger.info('Generating Win/Loss only frame')
        pnames = [value['person'] for key, value in players.items()]
        pnames.remove('none')
        self.weekly_frame = pd.DataFrame({'week_0': [1500] * len(pnames)}, index=pnames)

    def _load(self):
        try:
            self.weekly_frame = pd.read_csv('./resources/weekly_elo.csv', index_col=0)
        except FileNotFoundError:
            self._generate()
        except Exception as e:
            raise e

    def _verify(self):
        return 'week_{}'.format(self.week - 1) in self.weekly_frame.columns

    def _calc(self, frame):
        _logger.info('Starting win/loss only calculation')
        _logger.info('Creating blank new week')
        new_week = [0] * frame.shape[0]
        player_row_dict = {player: i for i, player in enumerate(self.weekly_frame.index)}
        calced = set()
        vals = frame['true_score']
        playoff = False
        for player_1_name in self.weekly_frame.index:
            if player_1_name not in calced:
                calced.add(player_1_name)
                p1_id = player_row_dict[player_1_name]
                player_2_name = frame['opponent'][player_1_name]
                try:
                    p2_id = player_row_dict[player_2_name]
                except KeyError:
                    _logger.info("It's the playoffs, and there's no opponent")
                    new_week[p1_id] = self.weekly_frame.iloc[p1_id, self.week - 1] * 1.0
                    playoff = True
                if not playoff:
                    _logger.info('Calculating for %s vs. %s', player_1_name, player_2_name)
                    player_1 = [self.weekly_frame.iloc[p1_id, self.week - 1] * 1.0, vals[player_1_name]]
                    player_2 = [self.weekly_frame.iloc[p2_id, self.week - 1] * 1.0, vals[player_2_name]]
                    scores = elo_calc(player_1, player_2, k=40)
                    _logger.info('Adding scores to new week')
                    new_week[p1_id] = scores[0]
                    new_week[p2_id] = scores[1]
                playoff = False
        _logger.info('Writing to frame')
        self.weekly_frame['week_{}'.format(self.week)] = new_week

    def _write(self):
        _logger.info('Updating weekly elos for week %s', self.week)
        self.weekly_frame.to_csv('./resources/weekly_elo.csv')

    def run(self, frame):
        if self.week:
            self._calc(frame)
        self._write()


# class StatEloCalc:
#     def __init__(self, week):
#         _logger.info('Elo calculator started')
#         if week:
#             _logger.info('Calculating Elos for week {}'.format(week))
#             self.week = week
#             self._load()
#             if not self._verify():
#                 _logger.info(
#                     'Error verifying. Make sure you have data for the previous week. Current week is {}'.format(week)
#                 )
#                 raise ValueError
#         else:
#             self._generate()
#
#     def _load(self):
#         try:
#             self.weekly_frame = pd.read_csv('./elo/weekly_stats_elo.csv', index_col=0)
#         except FileNotFoundError:
#             self._generate()
#         except Exception as e:
#             raise e
#
#     def _verify(self):
#         return sum(self.weekly_frame.week == 'week_{}'.format(self.week))
#
#     def _generate(self):
#         _logger.info('Generating week 0 frame')
#         with open(PLAYER_PICKLE_PATH, "rb") as pkl:
#             players = pickle.load(pkl)
#             _logger.info(players)
#         players = players[1:]
#         weekly_num = len(players) * 9
#         _logger.info('Generating Stats frame')
#         self.weekly_frame = pd.DataFrame({
#             'elo': [1500] * weekly_num,
#             'week': ['week_0'] * weekly_num,
#             'player': players * 9
#         }, index=range(weekly_num))
#
#     def _calc(self, frame):
#         _logger.info('Starting stats calculation')
#         _logger.info('Creating blank new week')
#         new_week = [0] * frame.shape[0]
#         player_row_dict = {player: i for i, player in enumerate(self.weekly_frame.index)}
#         calced = set()
#         vals = frame['true_score']
#         playoff = False
#         for player_1_name in self.weekly_frame.index:
#             if player_1_name not in calced:
#                 calced.add(player_1_name)
#                 p1_id = player_row_dict[player_1_name]
#                 player_2_name = frame['opponent'][player_1_name]
#                 try:
#                     p2_id = player_row_dict[player_2_name]
#                 except KeyError:
#                     _logger.info("It's the playoffs, and there's no opponent")
#                     new_week[p1_id] = self.weekly_frame.iloc[p1_id, self.week - 1] * 1.0
#                     playoff = True
#                 if not playoff:
#                     _logger.info('Calculating for %s vs. %s', player_1_name, player_2_name)
#                     player_1 = [self.weekly_frame.iloc[p1_id, self.week - 1] * 1.0, vals[player_1_name]]
#                     player_2 = [self.weekly_frame.iloc[p2_id, self.week - 1] * 1.0, vals[player_2_name]]
#                     scores = elo_calc(player_1, player_2, k=40)
#                     _logger.info('Adding scores to new week')
#                     new_week[p1_id] = scores[0]
#                     new_week[p2_id] = scores[1]
#                 playoff = False
#         _logger.info('Writing to frame')
#         self.weekly_frame['week_{}'.format(self.week)] = new_week
#
#     def _write(self):
#         _logger.info('Updating weekly elos for week %s', self.week)
#         self.weekly_frame.to_csv('./elo/weekly_elo.csv')
#
#     def run(self, frame):
#         if self.week:
#             self._calc(frame)
#         self._write()


if __name__ == "__main__":
    FRAME = pd.read_csv('./weekly_stats/week_{}.csv'.format(WEEK), index_col=0)
    calc = EloCalc(WEEK, False)
    calc.run(FRAME)
