ALTER TABLE connector_grants ADD COLUMN next_refresh_at timestamptz;
ALTER TABLE connector_grants ADD COLUMN refresh_failures integer NOT NULL DEFAULT 0;
