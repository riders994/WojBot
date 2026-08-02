-- Every league a manager plays in *this season*.
--
-- Drives the wizard's league step: a reporter in one league skips it, a
-- reporter in several has to say which one the rumor belongs to. Only the
-- current season counts, because only the current season can be reported on --
-- somebody who left a league last year would otherwise be asked to choose
-- between it and the one they actually play in, and picking the old one leads
-- nowhere (read_manager_season.sql is scoped the same way, so it would come
-- back with no team to speak for).
--
-- Read off dim_team rather than mvw_fact_league_managers: that view is all-time
-- membership with no year on it, so it cannot tell a current manager from a
-- departed one.
--
-- A co-manager counts as a manager of the team, here as everywhere else.
--
-- "This season" is the league's own latest league_year, matched by equality
-- rather than by picking a single online league: a year that ran on two
-- platforms has two dim_online_league rows, and both of them are this season.
SELECT DISTINCT
       l.league_id
     , l.league_name
     , l.discord_server_id
FROM fantasy_sports.dim_team t
JOIN fantasy_sports.dim_online_league ol
  ON ol.online_league_id = t.online_league_id
JOIN fantasy_sports.dim_league l
  ON l.league_id = ol.league_id
WHERE (t.manager_id = %(manager_id)s OR t.comanager_id = %(manager_id)s)
  AND ol.league_year = (
      SELECT MAX(cur.league_year)
      FROM fantasy_sports.dim_online_league cur
      WHERE cur.league_id = ol.league_id
  )
ORDER BY l.league_name;
