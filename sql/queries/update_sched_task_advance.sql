-- Move a task on after it has fired.
--
-- The whole outcome of one firing in a single statement: when it next fires
-- (NULL when the schedule is exhausted), whether it is still active, the
-- counters, and why it failed if it did. last_error is passed every time --
-- NULL on success -- so a task that recovers stops reporting an old failure.
--
-- fire_count counts *attempts*, not successes. A task that has tried and failed
-- five times has done something five times, and a counter that only moved on
-- success would make a broken task look like it had never run.
UPDATE fantasy_sports.sched_task
SET next_fire_at  = %(next_fire_at)s
  , status        = %(status)s
  , last_fired_at = %(fired_at)s
  , fire_count    = fire_count + 1
  , last_error    = %(last_error)s
  , updated_at    = now()
WHERE task_id = %(task_id)s
RETURNING task_id, status, next_fire_at;
