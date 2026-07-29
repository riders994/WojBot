-- The league bound to a Discord server.
--
-- The parameter is the *surrogate* for the guild id, not the real one --
-- real Discord ids never reach this database (see wojbot/core/discord_anon.py).
SELECT league_id
     , league_name
     , discord_server_id
FROM fantasy_sports.dim_league
WHERE discord_server_id = %(server_surrogate)s;
