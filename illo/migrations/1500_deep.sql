CREATE TABLE deep_runs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(),
 kind text NOT NULL CHECK (kind IN ('request','scout','brief')),
 state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','done','failed','cancelled')),
 job text NOT NULL, principal text NOT NULL,
 person_id bigint REFERENCES people(id), profile text NOT NULL,
 deliver jsonb NOT NULL, origin_event_id bigint REFERENCES events(id),
 run_id bigint REFERENCES runs(id), started_at timestamptz, ended_at timestamptz,
 lease_until timestamptz, result text, error text, dedupe_key text UNIQUE,
 delivered_at timestamptz,
 context jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX deep_queue_idx ON deep_runs (id) WHERE state='queued';
ALTER TABLE runs ADD COLUMN deep_run_id bigint REFERENCES deep_runs(id);
ALTER TABLE runs ADD COLUMN deep_kind text CHECK (deep_kind IN ('request','scout','brief'));
ALTER TABLE runs ADD COLUMN web_read boolean NOT NULL DEFAULT false;
ALTER TABLE runs ADD COLUMN outside_read boolean NOT NULL DEFAULT false;
ALTER TABLE runs ADD COLUMN tool_limit integer;
CREATE TABLE scout_findings (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(),
 scope text NOT NULL CHECK (scope IN ('team','person')),
 person_id bigint REFERENCES people(id), key text NOT NULL, title text NOT NULL,
 message text NOT NULL, links jsonb NOT NULL, for_person text,
 state text NOT NULL DEFAULT 'new' CHECK (state IN ('new','sent','task','dismissed')),
 deep_run_id bigint NOT NULL REFERENCES deep_runs(id),
 slack_channel text, slack_ts text, decided_by bigint REFERENCES people(id), decided_at timestamptz,
 CHECK ((scope='team' AND person_id IS NULL) OR (scope='person' AND person_id IS NOT NULL))
);
CREATE UNIQUE INDEX scout_scope_key_idx ON scout_findings (scope,COALESCE(person_id,0),key);
