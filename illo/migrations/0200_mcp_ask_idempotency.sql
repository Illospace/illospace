CREATE UNIQUE INDEX asks_token_source_ref_unique
ON asks (requester_token_id, source_ref)
WHERE requester_token_id IS NOT NULL AND source_ref IS NOT NULL;
