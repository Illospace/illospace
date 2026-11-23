CREATE TABLE memory_changes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL CHECK (kind IN ('note','list')),
    target text NOT NULL,
    title text NOT NULL,
    before_text text,
    after_text text NOT NULL,
    person_id bigint NOT NULL REFERENCES people,
    run_id bigint REFERENCES runs,
    slack_channel text,
    slack_ts text,
    thread_ts text,
    correction_ts text,
    state text NOT NULL DEFAULT 'saved' CHECK (state IN ('saved','corrected','forgotten')),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memory_changes_person_time ON memory_changes (person_id, created_at);
CREATE INDEX memory_changes_run ON memory_changes (run_id);
CREATE UNIQUE INDEX memory_changes_correction ON memory_changes (slack_channel, correction_ts)
    WHERE correction_ts IS NOT NULL;
