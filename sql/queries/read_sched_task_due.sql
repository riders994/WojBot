-- Every active task that is due, soonest first.
--
-- The scheduler's tick. Served by IX_sched_task_due, which is partial on
-- status = 'active' so the index holds only live tasks.
--
-- Due means next_fire_at <= now, not "= now": a task whose firing was missed
-- (the bot was down, a tick ran long) is still due, and the runner decides
-- whether it is too stale to deliver. The limit is a safety valve -- after a
-- long outage there may be a great many, and working through them a tick at a
-- time keeps one bad backlog from blocking the loop.
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
WHERE status = 'active'
  AND next_fire_at <= %(now)s
ORDER BY next_fire_at
LIMIT %(limit)s;
