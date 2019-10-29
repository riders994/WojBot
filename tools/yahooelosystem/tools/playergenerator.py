from selenium import webdriver
import logging
import json

URL = "https://basketball.fantasysports.yahoo.com/nba/{league}/matchup?week=1&module=matchup&mid1="
LEAGUE = '12682'
XPATH = '//section[@id="matchup-wall-header"]/table/tbody/tr'
PLAYER_SAVE_PATH = './data/players.json'
PLAYERS = 12


class PlayerGenerator:
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.getLevelName('INFO'))
    logger.info('Starting Job')
    player_dict = {'0': {'names': ['-/-'], 'person': 'none'}}

    def __init__(self, league):
        self.url = URL.format(league=league)

    def _read(self):
        try:
            with open(PLAYER_SAVE_PATH, "rb") as file:
                data = json.load(file)
            self.player_dict.update(data)
        except FileNotFoundError:
            pass

    def _connect(self):
        self.driver = webdriver.Firefox()

    def _scrape(self):
        for i in range(PLAYERS + 1):
            s = str(i)
            url = self.url + s
            self.driver.get(url)
            elem = self.driver.find_elements_by_xpath(XPATH)
            team = elem[0].text.split('\n')
            print(team)
            player = self.player_dict.get(s)
            curr = team[0]
            if player:
                names = player['names']
                names.append(curr)
                self.player_dict[s].update({'names': list(set(names)), 'current': curr})
            else:
                self.player_dict.update({s: {'names': [curr], 'person': curr, 'current': curr}})

    def _write(self):
        with open(PLAYER_SAVE_PATH, "w") as file:
            json.dump(self.player_dict, file)

    def run(self):
        self._read()
        self._connect()
        self._scrape()
        self._write()


if __name__ == '__main__':
    pg = PlayerGenerator(LEAGUE)
    pg.run()
