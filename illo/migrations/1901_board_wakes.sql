SET LOCAL lock_timeout = '1s';

ALTER TABLE board_wakes ADD COLUMN episode_key text;

CREATE UNIQUE INDEX board_wakes_episode ON board_wakes (thread_id, target_kind, episode_key)
    WHERE episode_key IS NOT NULL;

ALTER TABLE board_threads ADD COLUMN stalled_at timestamptz;

UPDATE board_threads SET stalled_at = updated_at
    WHERE stalled_reason = 'turn_cap' AND stalled_at IS NULL;
