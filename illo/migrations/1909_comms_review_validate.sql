SET LOCAL lock_timeout = '1s';

ALTER TABLE runs VALIDATE CONSTRAINT runs_profile_check;
