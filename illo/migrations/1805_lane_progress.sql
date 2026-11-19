SET LOCAL lock_timeout = '1s';

ALTER TABLE lanes ADD COLUMN IF NOT EXISTS progress jsonb;
ALTER TABLE lanes ADD COLUMN IF NOT EXISTS progress_at timestamptz;
