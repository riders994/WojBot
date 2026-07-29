-- Every release type, lowest authority first.
--
-- valid_forms lists the rumor forms this release type may be written in; the
-- level is what a source has to match or beat to be allowed to release it.
SELECT release_type_id
     , release_type_name
     , release_type_level
     , valid_forms
FROM fantasy_sports.dim_release_type
ORDER BY release_type_level, release_type_name;
