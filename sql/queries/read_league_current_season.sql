-- A league's most recent season.
--
-- What "/rumor recent" scopes to, and what read_manager_leagues.sql and
-- read_manager_season.sql mean by "this season". Read off dim_online_league
-- rather than anyone's team, because somebody without a team this year -- a
-- departed manager, a commissioner who doesn't play -- can still read the wire
-- from the league's own server, where the league comes from the channel.
SELECT online_league_id
     , league_year
FROM fantasy_sports.dim_online_league
WHERE league_id = %(league_id)s
ORDER BY league_year DESC
LIMIT 1;
