from selenium import webdriver
import logging
import json
import pickle

URL_PART_1 = "https://basketball.fantasysports.yahoo.com/nba/{league}/matchup?week="
URL_PART_2 = "{week}&module=matchup&mid1="
LEAGUE = '12682'
XPATH = '//section[@id="matchup-wall-header"]/table/tbody/tr'
PLAYER_INFO = './resources/players.json'
WEEK = '1'

with open(PLAYER_INFO, "rb") as file:
    PLAYERS = json.load(file)


logger = logging.getLogger(__name__)
logger.setLevel(logging.getLevelName('INFO'))
logger.info('Starting Job')


class YahooTableScraper:

    player_stats = dict()

    def __init__(self, league, players):
        self.url = URL_PART_1.format(league=league)
        self.players = players

        pass

    def _connect(self):
        self.driver = webdriver.Chrome()

    def _crawl(self, week):
        crawled = {'0', '-/-'}
        url_piece = self.url + URL_PART_2.format(week=week)
        for i in range(len(self.players)):
            place = str(i)
            if self.players[place]['current'] not in crawled:
                url = url_piece + place
                logger.info('Fetching game at url: ' + url)
                self.driver.get(url)
                logger.info('connecting')
                elem = self.driver.find_elements_by_xpath(XPATH)
                logger.info('Connected')
                teams = [el.text.split('\n') for el in elem]
                for j, el in enumerate(teams):
                    logger.info('Grab Player')
                    opp = teams[abs(j - 1)]
                    stats = el
                    stats.append(opp[0])
                    player_id = stats[0]
                    self.player_stats[player_id] = stats[1:]
                    crawled.add(player_id)
            else:
                logger.info('Player already grabbed')
        logger.info('Done Grabbing')
        self.driver.close()

    def run(self, week):
        self._connect()
        self._crawl(week)


if __name__ == "__main__":
    log = logging.getLogger('Test')
    s = YahooTableScraper(LEAGUE, PLAYERS)
    log.info('boop')
    s.run(WEEK)
    log.info('boop')
    for p, item in s.player_stats.items():
        log.info(p)
        log.info(item)
    with open('./data/player_stats.pkl', "wb") as file:
        pickle.dump(s.player_stats, file)
    log.info('boop')
