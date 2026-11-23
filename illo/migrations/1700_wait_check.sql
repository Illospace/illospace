ALTER TABLE deep_runs DROP CONSTRAINT deep_runs_kind_check;
ALTER TABLE deep_runs ADD CONSTRAINT deep_runs_kind_check
 CHECK (kind IN ('request','scout','brief','check'));
ALTER TABLE runs DROP CONSTRAINT runs_deep_kind_check;
ALTER TABLE runs ADD CONSTRAINT runs_deep_kind_check
 CHECK (deep_kind IN ('request','scout','brief','check'));
