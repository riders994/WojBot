-- One task by id, whatever its status.
--
-- Read before a cancel or a pause, so the bot can check who owns it and say
-- something specific when the answer is no.
SELECT task_id
     , task_kind
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
     , last_fired_at
     , fire_count
     , status
     , last_error
     , created_at
FROM fantasy_sports.sched_task
WHERE task_id = %(task_id)s;
