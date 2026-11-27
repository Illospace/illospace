SET LOCAL lock_timeout = '1s';

ALTER TABLE deep_runs DROP CONSTRAINT IF EXISTS deep_runs_kind_check;
ALTER TABLE deep_runs ADD CONSTRAINT deep_runs_kind_check
    CHECK (kind IN ('request', 'scout', 'brief', 'check', 'dream')) NOT VALID;

ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_deep_kind_check;
ALTER TABLE runs ADD CONSTRAINT runs_deep_kind_check
    CHECK (deep_kind IN ('request', 'scout', 'brief', 'check', 'dream')) NOT VALID;

CREATE TABLE IF NOT EXISTS dream_reflections (
    id bigserial PRIMARY KEY,
    deep_run_id bigint UNIQUE REFERENCES deep_runs(id),
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    summary text NOT NULL,
    scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    changes jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dream_reflections_created_idx
    ON dream_reflections (created_at DESC);
