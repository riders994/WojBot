-- One owner's live tasks, soonest first.
--
-- What `/remind list` shows in a DM: everything this person set, wherever they
-- set it. owner_id is the surrogate for their Discord id.
--
-- 'paused' is included with 'active': a paused task is still one they have set
-- and still something they may want to cancel or resume, so leaving it out
-- would make it unreachable.
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
WHERE owner_id = %(owner_id)s
  AND status IN ('active', 'paused')
ORDER BY next_fire_at NULLS LAST, task_id;
