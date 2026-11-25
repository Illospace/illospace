SET LOCAL lock_timeout = '1s';

-- 1. Two-person guest access for private cross-person board threads.
CREATE TABLE IF NOT EXISTS board_thread_guests (
    thread_id bigint NOT NULL REFERENCES board_threads ON DELETE RESTRICT,
    person_id bigint NOT NULL REFERENCES people ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (thread_id, person_id)
);
CREATE INDEX IF NOT EXISTS board_thread_guests_person
    ON board_thread_guests (person_id, thread_id);

-- 2. Store decision metadata on the post and allow code-applied standing rules
--    to record kind='decision' with explicit provenance='rule' (never an agent or model).
ALTER TABLE board_posts
    ADD COLUMN IF NOT EXISTS decision_spec jsonb;

ALTER TABLE board_posts DROP CONSTRAINT IF EXISTS board_posts_check1;
ALTER TABLE board_posts DROP CONSTRAINT IF EXISTS board_posts_decision_provenance_check;
ALTER TABLE board_posts ADD CONSTRAINT board_posts_decision_provenance_check CHECK (
    kind <> 'decision' OR (
        reply_to_post_id IS NOT NULL
        AND (is_human OR (actor = 'illo' AND provenance->>'via' = 'rule'))
    )
) NOT VALID;

-- 3. Route decision_needed through the single Chief-of-Staff episode and outbox pipeline.
ALTER TABLE board_episodes DROP CONSTRAINT IF EXISTS board_episodes_reason_check;
ALTER TABLE board_episodes ADD CONSTRAINT board_episodes_reason_check
    CHECK (reason IN ('question', 'mention', 'decision_needed', 'turn_cap')) NOT VALID;

ALTER TABLE board_coordinator_runs DROP CONSTRAINT IF EXISTS board_coordinator_runs_action_check;
ALTER TABLE board_coordinator_runs ADD CONSTRAINT board_coordinator_runs_action_check
    CHECK (action IS NULL OR action IN (
        'answer', 'route', 'ask_person', 'wait', 'headstart', 'escalate', 'nothing'
    )) NOT VALID;

ALTER TABLE board_escalations DROP CONSTRAINT IF EXISTS board_escalations_reason_check;
ALTER TABLE board_escalations ADD CONSTRAINT board_escalations_reason_check
    CHECK (reason IN ('question', 'mention', 'decision_needed', 'turn_cap')) NOT VALID;

ALTER TABLE board_escalations DROP CONSTRAINT IF EXISTS board_escalations_text_check;
ALTER TABLE board_escalations ADD CONSTRAINT board_escalations_text_check
    CHECK (char_length(text) BETWEEN 1 AND 2000) NOT VALID;

-- 4. Board-delivered decisions never use ask_pull/ask_ack; mark already-answered
--    board asks acked so digests do not report false delivery failures.
UPDATE asks SET delivery_state = 'acked'
    WHERE state = 'answered'
      AND delivery_state IN ('pending', 'failed')
      AND starts_with(COALESCE(source_ref, ''), 'board:');
