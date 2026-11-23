-- A backup restore builds each index with an empty search path.
-- illo_search_vector calls illo_search_tokens by its short name, so the restore failed.
-- Give the function its own search path. The result of the function does not change.
SET LOCAL lock_timeout = '1s';
ALTER FUNCTION illo_search_vector(text,text,text) SET search_path FROM CURRENT;
