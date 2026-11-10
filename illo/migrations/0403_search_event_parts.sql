-- Keep event search on a GIN index after the token change.
-- Use a separate transaction so this scan does not hold the items lock.
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';
CREATE INDEX IF NOT EXISTS events_slack_search_parts_idx ON events USING gin (
    illo_search_vector('','',coalesce(payload->>'text',''))
) WHERE channel='slack' AND principal='team';
