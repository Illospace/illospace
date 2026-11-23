SET LOCAL lock_timeout = '1s';

ALTER TABLE agent_oauth_requests
 ADD COLUMN IF NOT EXISTS slack_state_hash text,
 ADD COLUMN IF NOT EXISTS slack_nonce_hash text,
 ADD COLUMN IF NOT EXISTS consent_hash text,
 ADD COLUMN IF NOT EXISTS notice_at timestamptz;
