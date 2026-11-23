CREATE TABLE list_links (
    key_hash text PRIMARY KEY,
    person_id bigint NOT NULL REFERENCES people,
    expires_at timestamptz NOT NULL
);
CREATE INDEX list_links_person ON list_links (person_id);
