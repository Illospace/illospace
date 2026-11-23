ALTER TABLE connector_oauth_states ADD COLUMN provider text NOT NULL DEFAULT 'google';
UPDATE connector_oauth_states SET provider='chatgpt' WHERE connector_key='chatgpt';
