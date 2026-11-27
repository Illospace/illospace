SET LOCAL lock_timeout = '1s';

ALTER TABLE deep_runs VALIDATE CONSTRAINT deep_runs_kind_check;
ALTER TABLE runs VALIDATE CONSTRAINT runs_deep_kind_check;
