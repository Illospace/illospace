-- Backfill without a writer lock. Take the short writer locks only at the end.
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '5min';

-- Add foreign keys after the backfill. REFERENCES would lock the source table now.
CREATE TABLE item_search (item_id bigint PRIMARY KEY, vector tsvector NOT NULL);
CREATE TABLE slack_event_search (event_id bigint PRIMARY KEY, vector tsvector NOT NULL);

-- Keep each row version beside its vector. The final pass catches concurrent writes.
CREATE TEMP TABLE item_search_snapshot ON COMMIT DROP AS
    SELECT id,xmin::text AS version,illo_search_vector(title,summary,body) AS vector FROM items;
INSERT INTO item_search SELECT id,vector FROM item_search_snapshot;
CREATE INDEX items_search_vector_idx ON item_search USING gin (vector);
ANALYZE item_search;

CREATE TEMP TABLE slack_search_snapshot ON COMMIT DROP AS
    SELECT id,xmin::text AS version,
      illo_search_vector('','',coalesce(payload->>'text','')) AS vector
    FROM events WHERE channel='slack' AND principal='team' AND kind IN ('message','channel');
INSERT INTO slack_event_search SELECT id,vector FROM slack_search_snapshot;
CREATE INDEX slack_events_search_vector_idx ON slack_event_search USING gin (vector);
ANALYZE slack_event_search;

CREATE FUNCTION illo_item_search_vector() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    INSERT INTO item_search (item_id,vector)
    VALUES (NEW.id,illo_search_vector(NEW.title,NEW.summary,NEW.body))
    ON CONFLICT (item_id) DO UPDATE SET vector=EXCLUDED.vector;
    RETURN NULL;
END
$$;
CREATE FUNCTION illo_slack_event_search_vector() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF NEW.channel='slack' AND NEW.principal='team' AND NEW.kind IN ('message','channel') THEN
        INSERT INTO slack_event_search (event_id,vector)
        VALUES (NEW.id,illo_search_vector('','',coalesce(NEW.payload->>'text','')))
        ON CONFLICT (event_id) DO UPDATE SET vector=EXCLUDED.vector;
    ELSE
        DELETE FROM slack_event_search WHERE event_id=NEW.id;
    END IF;
    RETURN NULL;
END
$$;

-- Writers wait from this point until commit. Recompute only rows that changed.
LOCK TABLE items,events IN SHARE ROW EXCLUSIVE MODE;
SET LOCAL statement_timeout = '1s';
CREATE TRIGGER items_search_vector AFTER INSERT OR UPDATE OF title,summary,body ON items
    FOR EACH ROW EXECUTE FUNCTION illo_item_search_vector();
CREATE TRIGGER slack_events_search_vector
    AFTER INSERT OR UPDATE OF payload,channel,principal,kind ON events
    FOR EACH ROW EXECUTE FUNCTION illo_slack_event_search_vector();

DELETE FROM item_search s WHERE NOT EXISTS (SELECT 1 FROM items i WHERE i.id=s.item_id);
INSERT INTO item_search (item_id,vector)
    SELECT i.id,illo_search_vector(i.title,i.summary,i.body)
    FROM items i LEFT JOIN item_search_snapshot old ON old.id=i.id
    WHERE old.id IS NULL OR old.version<>i.xmin::text
    ON CONFLICT (item_id) DO UPDATE SET vector=EXCLUDED.vector;
DELETE FROM slack_event_search s WHERE NOT EXISTS (
    SELECT 1 FROM events e WHERE e.id=s.event_id
      AND e.channel='slack' AND e.principal='team' AND e.kind IN ('message','channel')
);
INSERT INTO slack_event_search (event_id,vector)
    SELECT e.id,illo_search_vector('','',coalesce(e.payload->>'text',''))
    FROM events e LEFT JOIN slack_search_snapshot old ON old.id=e.id
    WHERE e.channel='slack' AND e.principal='team' AND e.kind IN ('message','channel')
      AND (old.id IS NULL OR old.version<>e.xmin::text)
    ON CONFLICT (event_id) DO UPDATE SET vector=EXCLUDED.vector;

ALTER TABLE item_search ADD CONSTRAINT item_search_item_fk
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE NOT VALID;
ALTER TABLE item_search VALIDATE CONSTRAINT item_search_item_fk;
ALTER TABLE slack_event_search ADD CONSTRAINT slack_event_search_event_fk
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE NOT VALID;
ALTER TABLE slack_event_search VALIDATE CONSTRAINT slack_event_search_event_fk;

-- Stored vectors serve every reader. Keep the old generated column for row compatibility.
DROP INDEX IF EXISTS items_search_parts_idx;
DROP INDEX IF EXISTS items_tsv_idx;
DROP INDEX IF EXISTS events_slack_search_parts_idx;
