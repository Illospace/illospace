CREATE TABLE board_threads (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_post_at timestamptz NOT NULL DEFAULT now(),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resolved', 'archived')),
    visibility text NOT NULL DEFAULT 'team' CHECK (visibility IN ('team', 'private')),
    owner_person_id bigint REFERENCES people,
    created_by text NOT NULL,
    task_id bigint REFERENCES tasks ON DELETE RESTRICT,
    ticket_url text,
    consecutive_agent_posts integer NOT NULL DEFAULT 0,
    stalled_reason text CHECK (stalled_reason IS NULL OR stalled_reason IN ('turn_cap')),
    resolved_at timestamptz,
    resolved_by text,
    resolution_summary text NOT NULL DEFAULT '',
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title), 'A') ||
        setweight(to_tsvector('english', resolution_summary), 'B')
    ) STORED,
    CHECK (visibility <> 'private' OR owner_person_id IS NOT NULL)
);

CREATE INDEX board_threads_state_last ON board_threads (state, last_post_at DESC);
CREATE INDEX board_threads_task ON board_threads (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX board_threads_ticket ON board_threads (ticket_url) WHERE ticket_url IS NOT NULL;
CREATE INDEX board_threads_tsv ON board_threads USING gin (tsv);

-- One canonical open thread per work item and visibility scope (prevents duplicate races).
CREATE UNIQUE INDEX board_threads_open_task_team
    ON board_threads (task_id)
    WHERE state = 'open' AND visibility = 'team' AND task_id IS NOT NULL;
CREATE UNIQUE INDEX board_threads_open_task_private
    ON board_threads (task_id, owner_person_id)
    WHERE state = 'open' AND visibility = 'private' AND task_id IS NOT NULL;
CREATE UNIQUE INDEX board_threads_open_ticket_team
    ON board_threads (ticket_url)
    WHERE state = 'open' AND visibility = 'team' AND ticket_url IS NOT NULL;
CREATE UNIQUE INDEX board_threads_open_ticket_private
    ON board_threads (ticket_url, owner_person_id)
    WHERE state = 'open' AND visibility = 'private' AND ticket_url IS NOT NULL;

CREATE TABLE board_posts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_id bigint NOT NULL REFERENCES board_threads ON DELETE RESTRICT,
    at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL,
    is_human boolean NOT NULL DEFAULT false,
    person_id bigint REFERENCES people,
    token_id bigint REFERENCES tokens,
    provenance jsonb NOT NULL DEFAULT '{}',
    kind text NOT NULL CHECK (kind IN (
        'message', 'finding', 'question', 'answer', 'handoff', 'decision_needed', 'decision'
    )),
    text text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 16000),
    blocks jsonb NOT NULL DEFAULT '[]',
    mentions text[] NOT NULL DEFAULT '{}',
    reply_to_post_id bigint,
    ask_id bigint REFERENCES asks ON DELETE RESTRICT,
    source_ref text CHECK (source_ref IS NULL OR char_length(source_ref) BETWEEN 1 AND 128),
    request_hash text,
    UNIQUE (thread_id, id),
    FOREIGN KEY (thread_id, reply_to_post_id) REFERENCES board_posts (thread_id, id),
    CHECK (kind <> 'decision_needed' OR ask_id IS NOT NULL),
    CHECK (kind <> 'decision' OR (is_human AND reply_to_post_id IS NOT NULL))
);

CREATE INDEX board_posts_thread ON board_posts (thread_id, id);
CREATE UNIQUE INDEX board_posts_ask ON board_posts (ask_id) WHERE ask_id IS NOT NULL;
CREATE UNIQUE INDEX board_posts_idempotency
    ON board_posts (token_id, source_ref)
    WHERE token_id IS NOT NULL AND source_ref IS NOT NULL;

CREATE TABLE board_mentions (
    thread_id bigint NOT NULL,
    post_id bigint NOT NULL,
    target text NOT NULL,
    recipient_person_id bigint NOT NULL REFERENCES people,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, target),
    FOREIGN KEY (thread_id, post_id) REFERENCES board_posts (thread_id, id)
);

CREATE INDEX board_mentions_target ON board_mentions (target, recipient_person_id, created_at DESC);

-- Per-reader ack state: named agents never clear a person's shared inbox, and unnamed tokens do not clear each other or named agents.
CREATE TABLE board_mention_acks (
    post_id bigint NOT NULL,
    target text NOT NULL,
    reader_key text NOT NULL,
    acked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, target, reader_key),
    FOREIGN KEY (post_id, target) REFERENCES board_mentions (post_id, target)
);

CREATE TABLE board_wakes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_id bigint NOT NULL,
    post_id bigint NOT NULL,
    target_kind text NOT NULL CHECK (target_kind IN ('github_issue', 'webhook')),
    target_ref text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    attempts integer NOT NULL DEFAULT 0,
    due_at timestamptz NOT NULL DEFAULT now(),
    lease_id text,
    lease_until timestamptz,
    finished_at timestamptz,
    last_error text,
    UNIQUE (post_id, target_kind, target_ref),
    FOREIGN KEY (thread_id, post_id) REFERENCES board_posts (thread_id, id)
);

CREATE INDEX board_wakes_pending ON board_wakes (due_at) WHERE finished_at IS NULL;
