-- This table is created by 1000_connectors_oauth.sql.
ALTER TABLE connector_oauth_states ADD COLUMN cancelled_at timestamptz;
