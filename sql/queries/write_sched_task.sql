-- Record one scheduled task.
--
-- task_id, created_at and updated_at are left to the table (identity and now()
-- defaults) and the id is handed back so the bot can cite it.
--
-- status is passed rather than left to the table's 'active' default because not
-- every task starts running: a weekly league process is added switched off, and
-- an inactive row must also come in with next_fire_at NULL to stay out of the
-- poller's partial index (CK_sched_task_next_fire allows it either way).
--
-- Must be run through SqlService.run, never SqlService.execute: the payload
-- carries whatever the user typed, and execute interpolates with str.format.
-- Discord ids are surrogates (see wojbot/core/discord_anon.py), never real ones.
INSERT INTO fantasy_sports.sched_task
    ( task_kind
    , scope
    , league_id
    , owner_id
    , guild_id
    , channel_id
    , mention_ids
    , payload
    , schedule_kind
    , schedule_spec
    , timezone
    , next_fire_at
    , status
    )
VALUES
    ( %(task_kind)s
    , %(scope)s
    , %(league_id)s
    , %(owner_id)s
    , %(guild_id)s
    , %(channel_id)s
    , %(mention_ids)s
    , %(payload)s
    , %(schedule_kind)s
    , %(schedule_spec)s
    , %(timezone)s
    , %(next_fire_at)s
    , %(status)s
    )
RETURNING task_id, created_at;
