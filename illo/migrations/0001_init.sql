CREATE TABLE people (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(),
 key text NOT NULL UNIQUE, name text NOT NULL, slack_id text UNIQUE,
 github_login text UNIQUE, emails text[] NOT NULL DEFAULT '{}',
 role text NOT NULL DEFAULT 'member' CHECK (role IN ('owner','member')),
 timezone text NOT NULL DEFAULT 'UTC', areas text[] NOT NULL DEFAULT '{}',
 active boolean NOT NULL DEFAULT true
);
CREATE INDEX people_emails_idx ON people USING gin (emails);
CREATE TABLE tokens (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(),
 person_id bigint NOT NULL REFERENCES people(id), label text NOT NULL,
 token_hash text NOT NULL UNIQUE, scopes text[] NOT NULL DEFAULT '{}',
 last_used_at timestamptz, revoked_at timestamptz
);
CREATE TABLE events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), channel text NOT NULL,
 external_id text NOT NULL, kind text NOT NULL, principal text,
 person_id bigint REFERENCES people(id), tainted boolean NOT NULL DEFAULT false,
 payload jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'received' CHECK (status IN ('received','ignored','handled','failed')),
 UNIQUE (channel,external_id)
);
CREATE TABLE runs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), event_id bigint REFERENCES events(id),
 principal text NOT NULL, person_id bigint REFERENCES people(id),
 profile text NOT NULL CHECK (profile IN ('person_dm','team_channel','tainted','routine','agent_request')),
 tainted boolean NOT NULL DEFAULT false, personal_read boolean NOT NULL DEFAULT false,
 status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','timeout')),
 runtime text NOT NULL, model text NOT NULL, usage jsonb NOT NULL DEFAULT '{}', error text,
 started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
 token_hash text UNIQUE, token_expires_at timestamptz,
 CHECK ((principal LIKE 'person:%' AND person_id IS NOT NULL) OR
        (principal IN ('team','outsider') AND person_id IS NULL))
);
CREATE TABLE audit (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), run_id bigint REFERENCES runs(id),
 token_id bigint REFERENCES tokens(id), principal text NOT NULL, tool text NOT NULL,
 args_digest text NOT NULL, args jsonb NOT NULL DEFAULT '{}',
 decision text NOT NULL CHECK (decision IN ('allow','draft','deny')),
 reason text NOT NULL, at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_run_idx ON audit (run_id,at);
CREATE TABLE lanes (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(),
 source text NOT NULL CHECK (source IN ('t3','claude-code','codex','github','manual','email')),
 external_id text NOT NULL, title text NOT NULL,
 owner_person_id bigint REFERENCES people(id),
 state text NOT NULL CHECK (state IN ('working','waiting_person','blocked','review','done','failed','unknown')),
 summary text NOT NULL DEFAULT '', links jsonb NOT NULL DEFAULT '[]', repo text, machine text,
 visibility text NOT NULL DEFAULT 'team' CHECK (visibility IN ('team','private')),
 last_signal_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 closed_at timestamptz, end_summary text, slack_channel text, slack_ts text,
 UNIQUE (source,external_id), CHECK (visibility <> 'private' OR owner_person_id IS NOT NULL)
);
CREATE INDEX lanes_owner_state_updated_idx ON lanes (owner_person_id,state,updated_at DESC);
CREATE TABLE lane_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), lane_id bigint NOT NULL REFERENCES lanes(id),
 at timestamptz NOT NULL DEFAULT now(), kind text NOT NULL, text text NOT NULL,
 data jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX lane_events_lane_idx ON lane_events (lane_id,at DESC);
CREATE TABLE rules (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), owner_person_id bigint NOT NULL REFERENCES people(id),
 scope text NOT NULL CHECK (scope IN ('person','team')),
 kind text NOT NULL CHECK (kind IN ('always_allow','standing_answer')),
 match jsonb NOT NULL DEFAULT '{}', answer_option_label text, text text NOT NULL,
 origin text NOT NULL CHECK (origin IN ('card','file','proposal')),
 uses integer NOT NULL DEFAULT 0 CHECK (uses >= 0), removed_at timestamptz
);
CREATE INDEX rules_owner_active_idx ON rules (owner_person_id) WHERE removed_at IS NULL;
CREATE TABLE asks (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), lane_id bigint REFERENCES lanes(id),
 kind text NOT NULL CHECK (kind IN ('decision','action','review','reminder')),
 requester_token_id bigint REFERENCES tokens(id), requester_person_id bigint REFERENCES people(id),
 owner_person_id bigint NOT NULL REFERENCES people(id), question text NOT NULL,
 options jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(options) = 'array'),
 recommended_option text, proof jsonb NOT NULL DEFAULT '{}', due_at timestamptz, expires_at timestamptz,
 state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','answered','expired','cancelled')),
 answer_option text, answer_text text, answered_by_person_id bigint REFERENCES people(id),
 answered_via text CHECK (answered_via IN ('person','rule')), rule_id bigint REFERENCES rules(id),
 answered_at timestamptz,
 delivery_state text NOT NULL DEFAULT 'none' CHECK (delivery_state IN ('none','pending','delivered','acked','failed')),
 delivery_attempts integer NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
 delivered_at timestamptz, acked_at timestamptz, slack_channel text, slack_ts text,
 reminded_at timestamptz, source_ref text
);
CREATE INDEX asks_open_owner_idx ON asks (owner_person_id,due_at) WHERE state = 'open';
CREATE INDEX asks_delivery_idx ON asks (requester_token_id,delivery_state) WHERE state = 'answered';
CREATE INDEX asks_source_ref_idx ON asks (source_ref);
CREATE TABLE items (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(),
 kind text NOT NULL CHECK (kind IN ('saved','decision','note','lane_summary','archive','slack','github','doc')),
 source_ref text NOT NULL UNIQUE, title text NOT NULL, body text NOT NULL DEFAULT '',
 summary text NOT NULL DEFAULT '', owner_person_id bigint REFERENCES people(id),
 visibility text NOT NULL DEFAULT 'team' CHECK (visibility IN ('team','private')),
 links jsonb NOT NULL DEFAULT '[]', meta jsonb NOT NULL DEFAULT '{}', source_at timestamptz,
 tsv tsvector GENERATED ALWAYS AS (
   setweight(to_tsvector('simple',coalesce(title,'')) || to_tsvector('english',coalesce(title,'')),'A') ||
   setweight(to_tsvector('simple',coalesce(summary,'')) || to_tsvector('english',coalesce(summary,'')),'B') ||
   setweight(to_tsvector('simple',coalesce(body,'')) || to_tsvector('english',coalesce(body,'')),'C')
 ) STORED,
 CHECK (visibility <> 'private' OR owner_person_id IS NOT NULL)
);
CREATE INDEX items_tsv_idx ON items USING gin (tsv);
CREATE INDEX items_owner_visibility_idx ON items (owner_person_id,visibility);
CREATE TABLE submissions (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), event_id text NOT NULL UNIQUE,
 token_id bigint NOT NULL REFERENCES tokens(id), idempotency_key text,
 desired_outcome text NOT NULL, envelope jsonb NOT NULL, envelope_digest text NOT NULL,
 status text NOT NULL, result jsonb, completed_at timestamptz, run_id bigint REFERENCES runs(id),
 UNIQUE (token_id,idempotency_key)
);
CREATE TABLE drafts (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), run_id bigint NOT NULL REFERENCES runs(id),
 approver_person_id bigint NOT NULL REFERENCES people(id), kind text NOT NULL,
 destination text NOT NULL, content jsonb NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved','rejected','sent','expired')),
 slack_channel text, slack_ts text, decided_at timestamptz
);
CREATE TABLE signals (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), source text NOT NULL, dedupe_key text NOT NULL,
 severity text NOT NULL, title text NOT NULL, body text NOT NULL, data jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'received' CHECK (status IN ('received','ticketed','ignored')),
 ticket_url text, UNIQUE (source,dedupe_key)
);
CREATE TABLE claims (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), key text NOT NULL UNIQUE,
 run_id bigint NOT NULL REFERENCES runs(id), note text NOT NULL DEFAULT ''
);
CREATE TABLE connector_grants (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), connector_key text NOT NULL,
 person_id bigint REFERENCES people(id), secret bytea NOT NULL, scopes text[] NOT NULL DEFAULT '{}',
 expires_at timestamptz, revoked_at timestamptz,
 UNIQUE NULLS NOT DISTINCT (connector_key,person_id)
);
CREATE TABLE secrets (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), name text NOT NULL UNIQUE, secret bytea NOT NULL
);
CREATE TABLE routine_state (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), key text NOT NULL UNIQUE,
 last_run_at timestamptz, next_run_at timestamptz, last_status text
);
CREATE TABLE sensors (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), key text NOT NULL UNIQUE,
 last_ok_at timestamptz, last_error_at timestamptz, detail text NOT NULL DEFAULT ''
);
CREATE TABLE kv (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), key text NOT NULL UNIQUE, value jsonb NOT NULL
);
