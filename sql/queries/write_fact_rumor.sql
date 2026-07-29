-- Record one reported rumor.
--
-- rumor_id and reported_at are left to the table (identity and a now() default)
-- and handed back so the bot can cite the id it just created.
--
-- Must be run through SqlService.run, never SqlService.execute: rumor_text is
-- whatever the reporter typed, and execute interpolates with str.format.
INSERT INTO fantasy_sports.fact_rumor
    ( league_id
    , online_league_id
    , league_year
    , manager_id
    , rumor_form_id
    , source_type_id
    , release_type_id
    , rumor_text
    )
VALUES
    ( %(league_id)s
    , %(online_league_id)s
    , %(league_year)s
    , %(manager_id)s
    , %(rumor_form_id)s
    , %(source_type_id)s
    , %(release_type_id)s
    , %(rumor_text)s
    )
RETURNING rumor_id, reported_at;
