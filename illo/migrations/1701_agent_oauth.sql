SET LOCAL lock_timeout = '1s';

CREATE TABLE IF NOT EXISTS agent_oauth_clients (
 id text PRIMARY KEY, name varchar(80) NOT NULL, redirect_uris text[] NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), approved_at timestamptz
);
CREATE INDEX IF NOT EXISTS agent_oauth_clients_created_idx ON agent_oauth_clients(created_at);

CREATE TABLE IF NOT EXISTS agent_oauth_requests (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 client_id text NOT NULL REFERENCES agent_oauth_clients(id) ON DELETE CASCADE,
 redirect_uri text NOT NULL, resource text NOT NULL, state text, challenge text NOT NULL,
 pairing_hash text NOT NULL UNIQUE, browser_hash text NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '10 minutes',
 person_id bigint REFERENCES people(id), paired_at timestamptz,
 decision text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','approved','denied')),
 slack_channel text, slack_ts text, slack_event_ts text, continued_at timestamptz,
 token_id bigint REFERENCES tokens(id), code_hash text UNIQUE,
 code_expires_at timestamptz, exchanged_at timestamptz
);
CREATE INDEX IF NOT EXISTS agent_oauth_requests_client_idx ON agent_oauth_requests(client_id);
CREATE INDEX IF NOT EXISTS agent_oauth_requests_expiry_idx ON agent_oauth_requests(expires_at);

CREATE TABLE IF NOT EXISTS agent_oauth_access (
 token_hash text PRIMARY KEY, token_id bigint NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
 resource text NOT NULL, expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS agent_oauth_access_connection_idx ON agent_oauth_access(token_id);
CREATE INDEX IF NOT EXISTS agent_oauth_access_expiry_idx ON agent_oauth_access(expires_at);

CREATE TABLE IF NOT EXISTS agent_oauth_refresh (
 token_hash text PRIMARY KEY, token_id bigint NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
 client_id text NOT NULL REFERENCES agent_oauth_clients(id), resource text NOT NULL,
 seed text NOT NULL, expires_at timestamptz NOT NULL, used_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS agent_oauth_refresh_connection_idx ON agent_oauth_refresh(token_id);

CREATE TABLE IF NOT EXISTS agent_oauth_limits (
 kind text NOT NULL, key text NOT NULL, started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 attempts integer NOT NULL DEFAULT 1, PRIMARY KEY (kind,key)
);
