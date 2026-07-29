-- Every source type, most authoritative first.
--
-- source_type_name is a format string: it may contain {team}, {manager} or
-- {player}, filled in from whoever reported the rumor.
SELECT source_type_id
     , source_type_name
     , source_type_level
     , is_internal
FROM fantasy_sports.dim_source_type
ORDER BY source_type_level DESC, source_type_id;
