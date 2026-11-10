ALTER TABLE signals ADD COLUMN token_id bigint REFERENCES tokens(id);
CREATE INDEX signals_token_rate_idx ON signals (token_id, created_at);
CREATE INDEX submissions_token_rate_idx ON submissions (token_id, created_at)
    WHERE desired_outcome <> 'preserve_knowledge';
CREATE INDEX asks_token_open_idx ON asks (requester_token_id, owner_person_id)
    WHERE state = 'open';
