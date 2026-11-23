SET LOCAL lock_timeout = '1s';

ALTER TABLE asks ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;
