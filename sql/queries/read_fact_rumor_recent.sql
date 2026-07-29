-- A league's rumors for one season, newest first.
--
-- Everything needed to re-render is joined here: the release and source names,
-- the form's format string, and the reporter's team/display name *as of the
-- season the rumor was reported in* (fact_rumor.online_league_id), so an old
-- rumor keeps reading the way it read when it was posted.
--
-- The reporter's own identity is deliberately not selected. The source type is
-- the attribution; naming the manager behind it would give the game away.
SELECT r.rumor_id
     , r.reported_at
     , rt.release_type_name
     , rt.release_type_level
     , st.source_type_name
     , f.form_text
     , f.required_fills
     , r.rumor_text
     , t.team_name
     , mp.display_name
FROM fantasy_sports.fact_rumor r
JOIN fantasy_sports.dim_release_type rt
  ON rt.release_type_id = r.release_type_id
JOIN fantasy_sports.dim_source_type st
  ON st.source_type_id = r.source_type_id
JOIN fantasy_sports.dim_rumor_form f
  ON f.rumor_form_id = r.rumor_form_id
LEFT JOIN fantasy_sports.dim_online_league ol
  ON ol.online_league_id = r.online_league_id
LEFT JOIN fantasy_sports.dim_team t
  ON t.online_league_id = r.online_league_id
 AND (t.manager_id = r.manager_id OR t.comanager_id = r.manager_id)
LEFT JOIN fantasy_sports.dim_manager_platform mp
  ON mp.manager_id = r.manager_id
 AND mp.platform = ol.platform
WHERE r.league_id = %(league_id)s
  AND r.league_year = %(league_year)s
ORDER BY r.reported_at DESC, r.rumor_id DESC
LIMIT %(limit)s;
