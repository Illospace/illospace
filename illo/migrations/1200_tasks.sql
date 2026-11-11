ALTER TABLE tokens ADD COLUMN agent_key text;

CREATE TABLE tasks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    list_key text NOT NULL,
    title text NOT NULL,
    brief text NOT NULL,
    result text NOT NULL DEFAULT '',
    result_version integer NOT NULL DEFAULT 0,
    holder text,
    holder_since timestamptz,
    handed_by text,
    state text NOT NULL DEFAULT 'todo' CHECK (state IN ('todo','working','review','done','cancelled')),
    waits_for text,
    due_at timestamptz,
    parent_id bigint REFERENCES tasks,
    links jsonb NOT NULL DEFAULT '[]',
    created_by text NOT NULL,
    owner_person_id bigint REFERENCES people,
    visibility text NOT NULL DEFAULT 'team' CHECK (visibility IN ('team','private')),
    slack_channel text,
    slack_ts text,
    last_activity_at timestamptz NOT NULL DEFAULT now(),
    done_at timestamptz,
    done_by text,
    tsv tsvector GENERATED ALWAYS AS (
      setweight(to_tsvector('english', title), 'A') ||
      setweight(to_tsvector('english', brief), 'B') ||
      setweight(to_tsvector('english', result), 'C')) STORED
);
CREATE INDEX tasks_holder_state ON tasks (holder,state);
CREATE INDEX tasks_list_state ON tasks (list_key,state);
CREATE INDEX tasks_waits_for ON tasks (waits_for);
CREATE INDEX tasks_parent ON tasks (parent_id);
CREATE INDEX tasks_tsv ON tasks USING gin (tsv);

CREATE TABLE task_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id bigint NOT NULL REFERENCES tasks,
    at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('created','message','question','handed','state','result',
      'returned','link','done','reopened','cancelled','released','reminded')),
    text text NOT NULL DEFAULT '',
    data jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX task_events_task ON task_events (task_id,id);

ALTER TABLE audit ADD COLUMN target text;
