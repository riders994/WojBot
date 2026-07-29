-- The manager behind a Discord user.
--
-- The parameter is the *surrogate* for the user id, as text: discord_id is a
-- varchar, and rows the bot never linked hold an unrelated seed token rather
-- than a number. Empty result means nobody ran /commish db linkuser for them.
SELECT manager_id
     , player_name
FROM fantasy_sports.dim_manager
WHERE discord_id = %(manager_surrogate)s;
