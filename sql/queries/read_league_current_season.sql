-- A league's most recent season.
--
-- What "/rumor recent" scopes to. Read off dim_online_league rather than the
-- reporter's own team, because somebody without a team this year -- a departed
-- manager, a commissioner who doesn't play -- can still read the wire.
SELECT online_league_id
     , league_year
FROM fantasy_sports.dim_online_league
WHERE league_id = %(league_id)s
ORDER BY league_year DESC
LIMIT 1;
