SET LOCAL lock_timeout = '1s';

ALTER TABLE asks ADD COLUMN IF NOT EXISTS blocks jsonb NOT NULL DEFAULT '[]';

CREATE TABLE IF NOT EXISTS page_files (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 task_id bigint REFERENCES tasks(id) ON DELETE CASCADE,
 ask_id bigint REFERENCES asks(id) ON DELETE CASCADE,
 position integer NOT NULL, media_type text NOT NULL, data bytea NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK ((task_id IS NULL) <> (ask_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS page_files_task_idx ON page_files(task_id, position) WHERE task_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS page_files_ask_idx ON page_files(ask_id, position) WHERE ask_id IS NOT NULL;
