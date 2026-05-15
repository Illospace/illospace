-- Count runtime sync/async migration leftovers observed in AgentRun DB traces.
--
-- Usage:
--   psql "$DATABASE_URL" -f scripts/async_runtime_db_trace_metrics.sql
--
-- The zero target is for legacy_record_api_call_import and
-- sync_facade_in_async_loop. "other_tool_failure" is shown for context but is
-- not specifically a sync/async migration signal.

WITH error_events AS (
  SELECT
    e.run_id,
    r.thread_id,
    i.display_title,
    i.title,
    e.event_type,
    e.created_at,
    COALESCE(
      e.payload->>'error',
      e.payload->>'result',
      e.payload->>'label',
      e.payload::text
    ) AS text_payload
  FROM agent_run_events e
  JOIN agent_runs r ON r.id = e.run_id
  LEFT JOIN ideas i ON i.id::text = r.thread_id
  WHERE e.created_at >= now() - interval '7 days'
    AND (
      e.event_type IN ('run.tool_failed', 'run.error', 'run.failed')
      OR e.payload::text ILIKE '%cannot import name%'
      OR e.payload::text ILIKE '%active event loop%'
      OR e.payload::text ILIKE '%await the async%'
      OR e.payload::text ILIKE '%_record_api_call%'
      OR e.payload::text ILIKE '%sync facade%'
    )
),
classified AS (
  SELECT
    *,
    CASE
      WHEN text_payload ILIKE '%_record_api_call%' THEN 'legacy_record_api_call_import'
      WHEN text_payload ILIKE '%active event loop%'
        OR text_payload ILIKE '%await the async%'
        OR text_payload ILIKE '%sync facade%' THEN 'sync_facade_in_async_loop'
      WHEN text_payload ILIKE '%cannot import name%' THEN 'legacy_import_error'
      ELSE 'other_tool_failure'
    END AS category
  FROM error_events
)
SELECT
  category,
  COUNT(*) AS events,
  COUNT(DISTINCT run_id) AS runs,
  MIN(created_at) AS first_seen,
  MAX(created_at) AS last_seen
FROM classified
GROUP BY category
ORDER BY events DESC, category;

WITH error_events AS (
  SELECT
    e.run_id,
    r.thread_id,
    i.display_title,
    i.title,
    e.event_type,
    e.created_at,
    COALESCE(
      e.payload->>'error',
      e.payload->>'result',
      e.payload->>'label',
      e.payload::text
    ) AS text_payload
  FROM agent_run_events e
  JOIN agent_runs r ON r.id = e.run_id
  LEFT JOIN ideas i ON i.id::text = r.thread_id
  WHERE e.created_at >= now() - interval '7 days'
    AND (
      e.payload::text ILIKE '%active event loop%'
      OR e.payload::text ILIKE '%await the async%'
      OR e.payload::text ILIKE '%_record_api_call%'
      OR e.payload::text ILIKE '%sync facade%'
    )
)
SELECT
  run_id,
  COALESCE(display_title, title, '') AS title,
  event_type,
  created_at,
  LEFT(text_payload, 700) AS sample
FROM error_events
ORDER BY created_at DESC
LIMIT 50;
