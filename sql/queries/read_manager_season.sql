-- Who a manager is, in a league, in its most recent season.
--
-- This is what fills the {team} and {manager} placeholders in a source type:
-- the team name comes from that season's dim_team row, the display name from
-- the account they hold on whichever platform that season ran on. Both change
-- between seasons, which is why a rumor stores the season it was reported in.
--
-- A co-manager counts as the team's manager here -- they report rumors too.
-- display_name is LEFT JOINed: a manager with no account on that platform
-- still has a team, and the caller falls back rather than losing the row.
--
-- NB: display_name comes back tokenised by the elo package's anonymizer; the
-- caller reverses it (see wojbot.core.rumor.display_name).
SELECT ol.online_league_id
     , ol.league_year
     , ol.platform
     , t.team_name
     , mp.display_name
FROM fantasy_sports.dim_online_league ol
JOIN fantasy_sports.dim_team t
  ON t.online_league_id = ol.online_league_id
 AND (t.manager_id = %(manager_id)s OR t.comanager_id = %(manager_id)s)
LEFT JOIN fantasy_sports.dim_manager_platform mp
  ON mp.manager_id = %(manager_id)s
 AND mp.platform = ol.platform
WHERE ol.league_id = %(league_id)s
ORDER BY ol.league_year DESC
LIMIT 1;
