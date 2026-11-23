ALTER TABLE connector_grants ADD COLUMN broken_at timestamptz;
ALTER TABLE connector_grants ADD COLUMN notified_at timestamptz;
