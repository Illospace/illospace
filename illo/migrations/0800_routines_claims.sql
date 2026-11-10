-- A routine takes its claim before the gateway creates a run.
ALTER TABLE claims ALTER COLUMN run_id DROP NOT NULL;
