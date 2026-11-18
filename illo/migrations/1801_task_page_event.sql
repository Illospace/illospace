SET LOCAL lock_timeout = '1s';

-- The last task event that the page writer could see. A later return makes the page stale.
ALTER TABLE task_pages ADD COLUMN IF NOT EXISTS last_event_id bigint;
