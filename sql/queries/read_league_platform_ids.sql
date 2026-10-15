-- The platform league ids a league's seasons were played under.
--
-- How a dim_league row is matched back to a configured Elo league: each
-- ratings_*.yml names its seasons by the same (platform, platform_league_id)
-- pairs, which is also how elo_system itself resolves the row. Names are no
-- good for this -- /commish db rename changes dim_league's without touching
-- the YAML.
SELECT platform
     , platform_league_id
FROM fantasy_sports.dim_online_league
WHERE league_id = %(league_id)s;
