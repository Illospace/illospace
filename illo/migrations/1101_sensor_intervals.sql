-- Event sensors have no report interval.
ALTER TABLE sensors ADD COLUMN expected_every_s double precision CHECK (expected_every_s > 0);
