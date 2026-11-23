ALTER TABLE tasks ADD COLUMN alive_at timestamptz;
ALTER TABLE tasks ADD COLUMN stopped_at timestamptz;
ALTER TABLE task_events DROP CONSTRAINT task_events_kind_check;
ALTER TABLE task_events ADD CONSTRAINT task_events_kind_check CHECK (kind IN (
  'created','message','question','handed','state','result','returned','link','done',
  'reopened','cancelled','released','reminded','woken','wake_failed'));

CREATE TABLE task_wakes (
    event_id bigint PRIMARY KEY REFERENCES task_events,
    task_id bigint NOT NULL REFERENCES tasks,
    holder text NOT NULL,
    reason text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    due_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX task_wakes_pending ON task_wakes (due_at) WHERE finished_at IS NULL;
CREATE TABLE task_tickets (
    url text PRIMARY KEY,
    task_id bigint NOT NULL REFERENCES tasks
);

INSERT INTO task_tickets(url,task_id)
SELECT link->>'url',min(t.id) FROM tasks t, jsonb_array_elements(t.links) link
WHERE link->>'kind'='ticket' AND link->>'url' IS NOT NULL GROUP BY link->>'url';
