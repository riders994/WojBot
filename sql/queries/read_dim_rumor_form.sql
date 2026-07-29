-- Every rumor form.
--
-- form_text is the format string the finished rumor is rendered from, and
-- required_fills names the placeholders the reporter has to supply.
-- valid_sources restricts which sources may use the form; an empty array means
-- unrestricted, not "none" (form 1 is seeded '{}').
SELECT rumor_form_id
     , form_text
     , form_title
     , required_fills
     , valid_sources
FROM fantasy_sports.dim_rumor_form
ORDER BY rumor_form_id;
