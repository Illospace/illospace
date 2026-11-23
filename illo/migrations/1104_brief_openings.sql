CREATE INDEX events_brief_openings_idx ON events (channel,person_id,id DESC)
WHERE status='handled' AND jsonb_typeof(payload->'final_answer')='string'
  AND payload->>'final_answer'<>'';
