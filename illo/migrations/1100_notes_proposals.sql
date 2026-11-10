CREATE TABLE note_proposals (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(),
 owner_person_id bigint NOT NULL REFERENCES people(id),
 run_id bigint NOT NULL REFERENCES runs(id),
 title text NOT NULL, body text NOT NULL, about text NOT NULL DEFAULT '',
 state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','correcting','kept')),
 slack_channel text, slack_ts text, correction_ts text, file_name text,
 kept_at timestamptz
);
CREATE INDEX note_proposals_owner_state_idx ON note_proposals(owner_person_id,state);
CREATE UNIQUE INDEX note_proposals_thread_idx ON note_proposals(slack_channel,correction_ts)
 WHERE state='correcting';
