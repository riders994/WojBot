-- Every league a manager plays in.
--
-- Drives the wizard's league step: a reporter in one league skips it, a
-- reporter in several has to say which one the rumor belongs to.
SELECT l.league_id
     , l.league_name
     , l.discord_server_id
FROM fantasy_sports.mvw_fact_league_managers m
JOIN fantasy_sports.dim_league l
  ON l.league_id = m.league_id
WHERE m.manager_id = %(manager_id)s
ORDER BY l.league_name;
