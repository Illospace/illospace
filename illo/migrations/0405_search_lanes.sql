-- Fail fast behind a busy writer. Leave time for a slow disk during the backfill.
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '5min';

CREATE TABLE lane_search (
    lane_id bigint PRIMARY KEY REFERENCES lanes(id) ON DELETE CASCADE,
    vector tsvector NOT NULL
);
CREATE INDEX lane_events_recent_idx ON lane_events (lane_id,at DESC,id DESC);

CREATE FUNCTION illo_lane_search_vector() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    INSERT INTO lane_search (lane_id,vector)
    VALUES (NEW.id,illo_search_vector(NEW.title,'',concat_ws(' ',NEW.summary,(
        SELECT string_agg(recent.text,' ') FROM
          (SELECT text FROM lane_events WHERE lane_id=NEW.id ORDER BY at DESC,id DESC LIMIT 20) recent
    )))) ON CONFLICT (lane_id) DO UPDATE SET vector=EXCLUDED.vector;
    RETURN NULL;
END
$$;
CREATE TRIGGER lanes_search_vector AFTER INSERT OR UPDATE OF title,summary ON lanes
    FOR EACH ROW EXECUTE FUNCTION illo_lane_search_vector();

-- Serialize event writers before they change the last 20 events of the same lane.
CREATE FUNCTION illo_lane_event_search_lock() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM id FROM lanes WHERE id=NEW.lane_id FOR UPDATE;
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM id FROM lanes WHERE id=OLD.lane_id FOR UPDATE;
    ELSE
        PERFORM id FROM lanes WHERE id IN (OLD.lane_id,NEW.lane_id) ORDER BY id FOR UPDATE;
    END IF;
    RETURN coalesce(NEW,OLD);
END
$$;
CREATE TRIGGER lane_events_search_lock BEFORE INSERT OR UPDATE OR DELETE ON lane_events
    FOR EACH ROW EXECUTE FUNCTION illo_lane_event_search_lock();

CREATE FUNCTION illo_lane_event_search_refresh() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE lanes SET summary=summary WHERE id=NEW.lane_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE lanes SET summary=summary WHERE id=OLD.lane_id;
    ELSE
        UPDATE lanes SET summary=summary WHERE id IN (OLD.lane_id,NEW.lane_id);
    END IF;
    RETURN NULL;
END
$$;
CREATE TRIGGER lane_events_search_refresh AFTER INSERT OR UPDATE OR DELETE ON lane_events
    FOR EACH ROW EXECUTE FUNCTION illo_lane_event_search_refresh();

-- Use the same trigger for existing rows and future writes.
UPDATE lanes SET summary=summary;
CREATE INDEX lanes_search_parts_idx ON lane_search USING gin (vector);
