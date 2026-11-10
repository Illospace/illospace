CREATE TABLE connector_oauth_states (
 state_hash text PRIMARY KEY,
 connector_key text NOT NULL,
 person_id bigint REFERENCES people(id),
 secret bytea NOT NULL,
 expires_at timestamptz NOT NULL,
 started_at timestamptz,
 consumed_at timestamptz
);
