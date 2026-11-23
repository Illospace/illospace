SET LOCAL lock_timeout = '1s';
-- Step 1 of 2 (expand). The old code still runs on this schema: runtime is nullable and the
-- new columns are extra. A later migration drops runs.runtime.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS route text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS provider text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS payer text CHECK (payer IN ('api_key','chatgpt'));
ALTER TABLE runs ADD COLUMN IF NOT EXISTS payer_person_id bigint REFERENCES people(id);
ALTER TABLE runs ALTER COLUMN runtime DROP NOT NULL;
-- Fill the provider only where the old row proves it. An old API-key row can have used a
-- custom base URL, so it does not prove the openai provider.
UPDATE runs SET provider = CASE
    WHEN runtime = 'claude' THEN 'anthropic'
    WHEN runtime = 'openai_responses' AND usage->>'auth_kind' = 'chatgpt' THEN 'chatgpt'
    WHEN runtime = 'echo' THEN 'fake'
    ELSE 'unknown' END
WHERE provider IS NULL AND runtime IS NOT NULL;
-- The same rules as run_kind() in illo/brain/routes.py.
UPDATE runs r SET route = CASE
    WHEN e.channel = 'deep' THEN 'deep'
    WHEN r.tainted THEN 'tainted'
    WHEN e.channel IN ('slack','email') AND r.profile IN ('person_dm','team_channel') THEN r.profile
    WHEN e.channel = 'routine' THEN 'routine'
    WHEN e.channel IN ('digest','team_digest') THEN 'digest'
    WHEN e.channel = 'mcp' THEN 'agent_request'
    WHEN e.channel = 'signals' THEN 'tainted'
    END
FROM events e WHERE e.id = r.event_id AND r.route IS NULL AND r.runtime IS NOT NULL;
