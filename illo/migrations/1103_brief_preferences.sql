-- Each person controls only their own morning brief.
CREATE TABLE brief_preferences (
    person_id bigint PRIMARY KEY REFERENCES people(id),
    enabled boolean NOT NULL DEFAULT true,
    local_time text CHECK (local_time ~ '^([01][0-9]|2[0-3]):[0-5][05]$'),
    announced boolean NOT NULL DEFAULT false
);

-- Existing recipients already received a brief before this change.
INSERT INTO brief_preferences (person_id,announced)
SELECT p.id,true FROM people p JOIN routine_state r ON r.key='digest:' || p.key
WHERE r.last_run_at IS NOT NULL;
