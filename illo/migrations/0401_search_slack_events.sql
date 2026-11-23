-- Keep inbound message search small as the inbox grows.
CREATE INDEX events_slack_text_search ON events USING gin (
    (to_tsvector('simple',coalesce(payload->>'text','')) ||
     to_tsvector('english',coalesce(payload->>'text','')))
) WHERE channel='slack' AND principal='team';
