SET LOCAL lock_timeout = '1s';

-- 1. Thread state. Each column is nullable or has a constant default,
--    so the M2 image runs on this schema and never writes these columns.
--    stalled_at comes from M2 1901_board_wakes.sql (A4).
ALTER TABLE board_threads
    ADD COLUMN IF NOT EXISTS group_key text,
    ADD COLUMN IF NOT EXISTS closed_at timestamptz,
    ADD COLUMN IF NOT EXISTS reopened_at timestamptz,
    ADD COLUMN IF NOT EXISTS escalated_at timestamptz,
    ADD COLUMN IF NOT EXISTS coordinator_scanned_post_id bigint,
    ADD COLUMN IF NOT EXISTS coordinator_scanned_at timestamptz,
    ADD COLUMN IF NOT EXISTS coordinator_lease_id uuid,
    ADD COLUMN IF NOT EXISTS coordinator_lease_until timestamptz;

-- One rule in SQL and in Python: 2..48 characters, lowercase words split by ONE hyphen.
ALTER TABLE board_threads DROP CONSTRAINT IF EXISTS board_threads_group_key_check;
ALTER TABLE board_threads ADD CONSTRAINT board_threads_group_key_check
    CHECK (group_key IS NULL OR (group_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
                                 AND char_length(group_key) BETWEEN 2 AND 48)) NOT VALID;

-- M2 1901 already backfilled stalled_at and M2 writes it (A4). Keep this as a guard only.
UPDATE board_threads SET stalled_at = updated_at
    WHERE stalled_reason = 'turn_cap' AND stalled_at IS NULL;
UPDATE board_threads SET closed_at = COALESCE(resolved_at, updated_at)
    WHERE state IN ('resolved', 'archived') AND closed_at IS NULL;

CREATE INDEX IF NOT EXISTS board_threads_scan
    ON board_threads (last_post_at) WHERE state = 'open';
CREATE INDEX IF NOT EXISTS board_threads_group
    ON board_threads (group_key, visibility, owner_person_id, last_post_at DESC) WHERE group_key IS NOT NULL;

-- A6: the canonical ticket. The deploy backfills it in Python with canonical_ticket() (after it
--     removes '#fragment' and a trailing '/'). NULL rows fall back to the M1 _TICKET expression.
ALTER TABLE task_tickets ADD COLUMN IF NOT EXISTS canonical_url text;
CREATE INDEX IF NOT EXISTS task_tickets_canonical ON task_tickets (canonical_url);

-- 2. A coordinator run is a normal brain run with its own empty grant profile.
--    The 0001 check is unnamed, so Postgres named it runs_profile_check. The test checks the name.
--    VALIDATE runs in 1903, because the runner wraps each file in one transaction.
ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_profile_check;
ALTER TABLE runs ADD CONSTRAINT runs_profile_check CHECK (profile IN
    ('person_dm', 'team_channel', 'tainted', 'routine', 'agent_request', 'board_coordinator')) NOT VALID;

-- An older image can still read and display the delivered card.
ALTER TABLE asks DROP CONSTRAINT IF EXISTS asks_board_answer_via_check;
ALTER TABLE asks ADD CONSTRAINT asks_board_answer_via_check CHECK (
    allow_rule OR NOT COALESCE(starts_with(source_ref, 'board:'), false)
    OR COALESCE(answered_via, 'person') = 'person'
) NOT VALID;

-- 3. One episode for each thing that waits for help: a pending question, a pending
--    person mention, or one turn-cap stall. The coordinator schedules episodes, not posts.
CREATE TABLE IF NOT EXISTS board_episodes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    thread_id bigint NOT NULL REFERENCES board_threads ON DELETE RESTRICT,
    reason text NOT NULL CHECK (reason IN ('question', 'mention', 'turn_cap')),
    post_id bigint,                       -- question and mention: the agent post
    mention_target text,                  -- mention: 'person:<key>'
    stalled_at timestamptz,               -- turn_cap: the stall start
    episode_seq smallint NOT NULL DEFAULT 1,  -- +1 for each new episode of the same item (after stuck or reopen)
    state text NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'waiting', 'headstart', 'escalated', 'closed', 'stuck')),
    next_check_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempts smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 10),
    waits smallint NOT NULL DEFAULT 0,      -- consecutive `wait` decisions
    errors smallint NOT NULL DEFAULT 0,     -- invalid decisions + failed runs
    headstart_target text CHECK (headstart_target IS NULL OR headstart_target ~ '^(agent|person):[a-z][a-z0-9_-]*$'),
    headstart_until timestamptz,
    version integer NOT NULL DEFAULT 0,     -- +1 on each change; the apply checks it
    closed_at timestamptz,
    closed_reason text CHECK (closed_reason IS NULL OR closed_reason IN
        ('answered', 'no_longer_pending', 'thread_closed', 'acl_lost', 'fallback_sent', 'stuck')),
    FOREIGN KEY (thread_id, post_id) REFERENCES board_posts (thread_id, id),
    CHECK ((reason = 'turn_cap') = (stalled_at IS NOT NULL)),
    CHECK ((reason = 'turn_cap') = (post_id IS NULL)),
    CHECK ((reason = 'mention') = (mention_target IS NOT NULL))
);
-- One ACTIVE episode per item. 'closed' and 'stuck' rows do not block a new episode
-- (after a new post or a reopen). 'headstart' is active, so it is in the set.
CREATE UNIQUE INDEX IF NOT EXISTS board_episodes_item
    ON board_episodes (thread_id, reason, post_id, COALESCE(mention_target, ''))
    WHERE reason <> 'turn_cap' AND state IN ('open', 'waiting', 'headstart', 'escalated');
CREATE UNIQUE INDEX IF NOT EXISTS board_episodes_stall
    ON board_episodes (thread_id, stalled_at)
    WHERE reason = 'turn_cap' AND state IN ('open', 'waiting', 'headstart', 'escalated');
CREATE UNIQUE INDEX IF NOT EXISTS board_episodes_seq
    ON board_episodes (thread_id, reason, COALESCE(post_id, 0), COALESCE(mention_target, ''),
                       COALESCE(stalled_at, 'epoch'), episode_seq);
CREATE INDEX IF NOT EXISTS board_episodes_due
    ON board_episodes (next_check_at) WHERE state IN ('open', 'waiting', 'headstart', 'escalated');
CREATE INDEX IF NOT EXISTS board_episodes_thread ON board_episodes (thread_id, state);

-- 4. Each ADMITTED model attempt. A skipped attempt (budget) writes no row.
--    `snapshot` holds IDs, keys and flags only, never post text. There is no model note.
CREATE TABLE IF NOT EXISTS board_coordinator_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    thread_id bigint NOT NULL REFERENCES board_threads ON DELETE RESTRICT,
    episode_id bigint NOT NULL REFERENCES board_episodes ON DELETE RESTRICT,
    attempt smallint NOT NULL CHECK (attempt BETWEEN 1 AND 10),   -- = max of board.coordinator.max_attempts_per_episode
    lease_id uuid NOT NULL,
    event_id bigint REFERENCES events,
    run_id bigint REFERENCES runs,
    snapshot jsonb NOT NULL DEFAULT '{}',
    action text CHECK (action IS NULL OR action IN ('wait', 'headstart', 'escalate', 'nothing')),
    outcome text NOT NULL DEFAULT 'running'
        CHECK (outcome IN ('running', 'applied', 'stale', 'invalid', 'failed')),
    error_code text CHECK (error_code IS NULL OR error_code ~ '^[a-z_]{1,40}$'),  -- a fixed code, never model text
    finished_at timestamptz,
    UNIQUE (episode_id, attempt)
);
CREATE INDEX IF NOT EXISTS board_coordinator_runs_recent ON board_coordinator_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS board_coordinator_runs_thread ON board_coordinator_runs (thread_id, created_at DESC);

-- 5. Each message that Illo sends to a person, and the Slack root that a reply attaches to.
CREATE TABLE IF NOT EXISTS board_escalations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    thread_id bigint NOT NULL REFERENCES board_threads ON DELETE RESTRICT,
    episode_id bigint NOT NULL REFERENCES board_episodes ON DELETE RESTRICT,
    post_id bigint,                                   -- NULL for a turn_cap episode
    reason text NOT NULL CHECK (reason IN ('question', 'mention', 'turn_cap')),
    origin text NOT NULL CHECK (origin IN ('model', 'fallback')),
    coordinator_run_id bigint REFERENCES board_coordinator_runs,
    person_id bigint NOT NULL REFERENCES people,
    format text NOT NULL CHECK (format IN ('dm', 'card', 'link')),
    content_mode text NOT NULL CHECK (content_mode IN ('notice', 'summary')),
    text text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 600),   -- already cleaned (§6.0)
    card jsonb,                                       -- card: {question, options, recommended}, cleaned
    ask_id bigint REFERENCES asks ON DELETE RESTRICT, -- card: set in the same transaction as the ask insert (§6.1)
    card_ask_id bigint,                               -- card: the reserved ask ID (no FK; the ask row comes after Slack)
    due_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_id uuid, lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    send_started_at timestamptz,
    slack_channel text, slack_ts text,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'sending', 'sent', 'answered', 'closed', 'failed')),
    closed_reason text CHECK (closed_reason IS NULL OR closed_reason IN
        ('other_answer', 'no_longer_pending', 'thread_closed', 'ask_expired', 'ask_cancelled', 'acl_lost')),
    sent_at timestamptz, closed_at timestamptz,
    answer_post_id bigint,
    recipient_slack_user text,
    sending_channel text,
    notice_at timestamptz,
    closed_by_person_id bigint REFERENCES people,
    acl_snapshot jsonb NOT NULL DEFAULT '{}',
    FOREIGN KEY (thread_id, post_id) REFERENCES board_posts (thread_id, id),
    FOREIGN KEY (thread_id, answer_post_id) REFERENCES board_posts (thread_id, id),
    CHECK ((slack_channel IS NULL) = (slack_ts IS NULL)),
    CHECK (format <> 'card' OR card IS NOT NULL),
    CHECK (format <> 'card' OR content_mode = 'summary'),
    CHECK (format <> 'card' OR slack_ts IS NULL),     -- a card root belongs to its ask, never to this row
    CHECK ((reason = 'turn_cap') = (post_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS board_escalations_slack
    ON board_escalations (slack_channel, slack_ts) WHERE slack_ts IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS board_escalations_once
    ON board_escalations (episode_id, person_id);
CREATE UNIQUE INDEX IF NOT EXISTS board_escalations_ask
    ON board_escalations (ask_id) WHERE ask_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS board_escalations_due
    ON board_escalations (due_at) WHERE state IN ('pending', 'sending');
CREATE INDEX IF NOT EXISTS board_escalations_person
    ON board_escalations (person_id, created_at DESC);
CREATE INDEX IF NOT EXISTS board_escalations_open
    ON board_escalations (thread_id) WHERE state IN ('pending', 'sending', 'sent');

CREATE UNIQUE INDEX IF NOT EXISTS board_posts_untokened_source
    ON board_posts (actor, source_ref)
    WHERE token_id IS NULL AND source_ref IS NOT NULL;
