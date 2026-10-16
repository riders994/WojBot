-- Every live task pointed at one server, soonest first.
--
-- What `/remind list` shows inside a server, for anyone who can see the
-- channels it posts to -- a channel reminder is public by nature, so hiding
-- other people's from the list would only mean nobody could work out where a
-- recurring post was coming from. guild_id is the surrogate for the server id.
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
WHERE guild_id = %(guild_id)s
  AND status IN ('active', 'paused')
ORDER BY next_fire_at NULLS LAST, task_id;
