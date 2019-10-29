from yahooelosystem.tools.yahoo_table_scraper import YahooTableScraper
from yahooelosystem.tools.weekly_formatter import WeeklyFormatter
from yahooelosystem.tools.elo_calculator import EloCalc
import logging
import argparse
import json


parser = argparse.ArgumentParser()

LEAGUE = '12682'
WEEK = '1'
PLAYER_INFO = './resources/players.json'

with open(PLAYER_INFO, "rb") as data:
    PLAYERS = json.load(data)


def week_formatter(week):
    s = week.split(':')
    if len(s) - 1:
        return range(int(s[0]), int(s[-1]) + 1), True
    else:
        return int(week), False


class YahooEloSystem:

    def __init__(self, league, week, players, stats=False):
        self.league = league
        self.players = players
        self.week, self.multi = week_formatter(week)
        self.scraper = YahooTableScraper(self.league, self.players)
        self.stats = stats
        self.logger = logging.getLogger(__name__)

    def _scrape(self):
        self.scraper.run(self.week)

    def _format(self, scraper):
        self.formatter = WeeklyFormatter(self.week)
        self.formatter.run(scraper)

    def _elo(self):
        self.elo_calc = EloCalc(self.week)
        self.elo_calc.run(self.formatter.frame)

    def run_multiple(self):
        self.multi = False
        week_stored = self.week
        for i in week_stored:
            self.week = i
            self.run()

    def run(self):
        if self.multi:
            self.run_multiple()
        else:
            self._scrape()
            self._format(self.scraper.player_stats)
            self._elo()


if __name__ == "__main__":
    sys = YahooEloSystem(LEAGUE, WEEK, PLAYERS)
    sys.run()
