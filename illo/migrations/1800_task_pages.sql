SET LOCAL lock_timeout = '1s';

CREATE TABLE IF NOT EXISTS web_signins (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 pairing_hash text NOT NULL UNIQUE, browser_hash text NOT NULL UNIQUE,
 next_path text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '10 minutes',
 person_id bigint REFERENCES people(id), paired_at timestamptz,
 decision text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','approved','denied')),
 slack_channel text, slack_ts text, slack_event_ts text, continued_at timestamptz
);
CREATE INDEX IF NOT EXISTS web_signins_expiry_idx ON web_signins(expires_at);
CREATE TABLE IF NOT EXISTS web_sessions (
 token_hash text PRIMARY KEY, person_id bigint NOT NULL REFERENCES people(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '30 days',
 last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS web_sessions_expiry_idx ON web_sessions(expires_at);
CREATE TABLE IF NOT EXISTS task_pages (
 task_id bigint PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
 blocks jsonb NOT NULL, author text NOT NULL, result_version integer NOT NULL,
 written_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
