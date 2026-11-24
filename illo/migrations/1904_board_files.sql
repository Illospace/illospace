SET LOCAL lock_timeout = '1s';

ALTER TABLE page_files ADD COLUMN IF NOT EXISTS board_post_id bigint
    REFERENCES board_posts(id) ON DELETE CASCADE;
ALTER TABLE page_files DROP CONSTRAINT IF EXISTS page_files_check;
ALTER TABLE page_files DROP CONSTRAINT IF EXISTS page_files_one_owner;
ALTER TABLE page_files ADD CONSTRAINT page_files_one_owner
    CHECK (num_nonnulls(task_id, ask_id, board_post_id) = 1) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS page_files_board_idx
    ON page_files (board_post_id, position) WHERE board_post_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS board_threads_closed_files_idx
    ON board_threads (closed_at, id) WHERE state IN ('resolved', 'archived');
