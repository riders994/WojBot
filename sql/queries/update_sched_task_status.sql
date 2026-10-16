-- Cancel, pause, or resume a task.
--
-- Cancelling is a status change rather than a delete, so a task that fired
-- eleven times before somebody stopped it still says so. next_fire_at is passed
-- alongside because the two have to move together: CK_sched_task_next_fire
-- requires an active task to know when it fires next, and a cancelled one must
-- drop out of the poller's partial index.
UPDATE fantasy_sports.sched_task
SET status       = %(status)s
  , next_fire_at = %(next_fire_at)s
  , updated_at   = now()
WHERE task_id = %(task_id)s
RETURNING task_id, status, next_fire_at;
