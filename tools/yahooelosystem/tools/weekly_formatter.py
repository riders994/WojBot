import pandas as pd
import numpy as np
import logging
import pickle
import json


PLAYER_INFO = './resources/players.json'
PLAYER_PICKLE_PATH = './player_stats.pkl'
INIT_COLS = ['fgmfga', 'fgpct', 'ftmfta', 'ftpct', 'threes', 'points', 'rebounds', 'assists', 'steals', 'blocks',
             'turnovers', 'score', 'opponent']
KEEP_COLS = ['fgm', 'fga', 'fgpct', 'ftm', 'fta', 'ftpct', 'threes', 'points', 'rebounds', 'assists', 'steals', 'blocks'
             , 'turnovers', 'true_score', 'opponent', 'week']
INT_COLS = ['fgm', 'fga', 'ftm', 'fta', 'threes', 'points', 'rebounds', 'assists', 'steals', 'blocks', 'turnovers',
            'week']
ROTO_COLS = ['fgpct', 'ftpct', 'threes', 'points', 'rebounds', 'assists', 'steals', 'blocks', 'turnovers']
FLT_COLS = ['fgpct', 'ftpct', 'score']
WEEK = 1

_logger = logging.getLogger(__name__)


def _purge_stars(column):
    return column.str.replace('*', '', regex=False)


class WeeklyFormatter:
    frame = None
    roto = None

    def __init__(self, week):
        _logger.setLevel(logging.getLevelName('INFO'))
        _logger.info('Formatter started')
        self.week = week
        self.score_dict = dict()
        self.player_info = self._read()

    @staticmethod
    def _read():
        with open(PLAYER_INFO, "rb") as data:
            players = json.load(data)
        return {value['current']: value['person'] for key, value in players.items()}

    def _fix_scores(self, frame):
        _logger.info('Fixing scores')
        frame['true_score'] = 0.0
        scored = set()
        for team in frame.index:
            if team not in scored:
                home = frame.loc[team]
                away = home.opponent
                _logger.info('Fixing scores for {home} and {away} for week {week}'.format(home=team, away=away,
                                                                                          week=self.week))
                home_score = home.score * 1.0
                try:
                    away_score = frame.loc[away].score * 1.0
                except KeyError:
                    away_score = 0
                except Exception as e:
                    raise e
                diff = (9 - home_score - away_score)/2
                home_score += diff
                away_score += diff
                home_score /= 9
                away_score /= 9
                _logger.info('boop')
                frame['true_score'][team] = home_score
                _logger.info('boop')
                frame['true_score'][away] = away_score
                _logger.info('boop')
                scored.add(team)
                scored.add(away)
                _logger.info('Fixed scores for {home} and {away} for week {week}'.format(home=team, away=away, week=self.week))
        return frame[KEEP_COLS]

    def _format(self, player_dict):
        frame = pd.DataFrame.from_dict(player_dict, orient='index', columns=INIT_COLS)
        frame.index = [self.player_info[ind] for ind in frame.index]
        frame.opponent.replace(self.player_info, inplace=True)
        _logger.info('Loaded frame')
        for col in ['-/-', '0']:
            try:
                frame.drop(index=col, inplace=True)
            except KeyError:
                pass
            except Exception as e:
                raise e
        fg = frame.fgmfga.str.split('/')
        ft = frame.ftmfta.str.split('/')
        _logger.info('Splitting field goals and free throws')
        frame[['fgm', 'fga']] = pd.DataFrame(fg.tolist(), index=frame.index)
        frame[['ftm', 'fta']] = pd.DataFrame(ft.tolist(), index=frame.index)
        frame['week'] = self.week
        for col in INT_COLS:
            frame[col] = frame[col].astype(int)
            _logger.info(col)
        for col in FLT_COLS:
            try:
                frame[col] = frame[col].astype(float)
            except ValueError:
                column = _purge_stars(frame[col])
                frame[col] = column.astype(float)
            except Exception as e:
                raise e

        self.frame = self._fix_scores(frame)
        _logger.info('Scores fixed')

    def _roto(self):
        roto_scores = self.frame[ROTO_COLS].values.argsort(axis=0).argsort(axis=0) + 1
        rotos = roto_scores.sum(axis=1)
        self.frame['roto'] = rotos
        self.frame['roto_rank'] = rotos.argsort(axis=0).argsort(axis=0) + 1

    def _write(self):
        self.frame.to_csv('./resources/week_{}.csv'.format(self.week))

    def run(self, player_dict, roto=True):
        if self.week:
            _logger.info('Loading testing dictionary.')
            self._format(player_dict)
            if roto:
                self._roto()
            self._write()


if __name__ == '__main__':
    with open('./data/player_stats.pkl', 'rb') as file:
        stats_dict = pickle.load(file)

    frm = WeeklyFormatter(WEEK)
    frm.run(stats_dict)
