CREATE TABLE item_aliases (
 alias text PRIMARY KEY,
 item_id bigint NOT NULL REFERENCES items(id) ON DELETE CASCADE
);
CREATE INDEX item_aliases_item_idx ON item_aliases(item_id);
