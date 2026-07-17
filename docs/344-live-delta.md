# Issue #344 deploy note — Domain 37 memory contract

This branch performs no live, SSH, or migration write. After the commit lands,
deployment must apply the exact Domain `37` changes below. The embeddings and
recall-ranking slice from #342 has already merged; that sequencing is a soft
dependency so the first coordinator recalls are relevant, not an activation
blocker.

## 1. Create or update the on-demand memory record

On a fresh install, create a `doc_page` with:

- intended record id: `1275` (the core pointer is pinned to this id; assert the
  returned id before continuing);
- slug: `uwear-engineering-triage-memory`;
- title: `Uwear Engineering Triage — Memory Playbook`; and
- content: the exact UTF-8 contents of
  `brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/references/memory.md`.

Expected content fingerprint:

- characters: `4886`;
- bytes: `4912`; and
- SHA-256: `5fff971e13dea82cca19fce1f36a8d340904842a0398dd66ded4c371cd0de165`.

Read record `1275` back and require byte identity with the bundled reference.
If record `1275` already has slug `uwear-engineering-triage-memory`, update its
content with the current `expected_version`; if the id is occupied by another
slug, stop. Do not update core record `1155` to point at a missing or different
playbook.

## 2. Replace core record 1155 with doc v9

Read Domain `37` `doc_page` record `1155` and verify slug
`uwear-engineering-triage`. It is expected to be v8 immediately before this
activation. Update by `record_id=1155` with the just-read `expected_version`,
preserving all record fields except:

- set `data.content` to the exact UTF-8 contents of
  `brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/SKILL.md`.

This is a whole-content replacement, not a hand-edited approximation. Expected
v9 fingerprint:

- characters: `33554` (must remain `< 34000`);
- bytes: `33686`; and
- SHA-256: `8321b96e0c1f8fe293e101a58e040c7b51a81cda9db37b5cbe564ef31d118866`.

If record `1155` is no longer v8, re-read and reconcile the newer live edit
into the bundle before writing. Never silently overwrite it.

Record the activation time before waiting for acceptance runs:

```bash
ACTIVATED_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%s\n' "$ACTIVATED_AT_UTC"
```

## 3. SSH byte-identity verification

From the deployed checkout on the operator workstation, set the SSH target.
The helper extracts `data->>'content'` as base64 so `psql` cannot add a row
terminator that would make a byte comparison lie:

```bash
export ILLO_SSH='<ssh-user>@<server>'

fetch_domain_content() {
  local record_id="$1"
  ssh "$ILLO_SSH" "bash -s -- '$record_id'" <<'REMOTE'
set -euo pipefail
record_id="$1"
[[ "$record_id" =~ ^[0-9]+$ ]]
cd ~/illospace
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml \
  exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<SQL | tr -d '\r\n' | base64 -d
SELECT encode(convert_to(data->>'content', 'UTF8'), 'base64')
FROM domain_records
WHERE domain_id = 37 AND id = ${record_id};
SQL
REMOTE
}

fetch_domain_content 1155 > /tmp/domain-37-1155.md
fetch_domain_content 1275 > /tmp/domain-37-1275.md

cmp -s \
  brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/SKILL.md \
  /tmp/domain-37-1155.md
cmp -s \
  brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/references/memory.md \
  /tmp/domain-37-1275.md
shasum -a 256 /tmp/domain-37-1155.md /tmp/domain-37-1275.md
```

Both `cmp` commands must exit `0`, and the hashes must equal the fingerprints
above. Also read both records normally and confirm record `1155` is v9, record
`1275` has the expected slug, and the core pointer names record `1275` plus
`references/memory.md`.

## 4. Cycle-2 acceptance

Wait for the next three completed scheduled cycle-2 runs after
`ACTIVATED_AT_UTC`, then run:

```bash
ssh "$ILLO_SSH" "bash -s -- '$ACTIVATED_AT_UTC'" <<'REMOTE'
set -euo pipefail
activated_at="$1"
cd ~/illospace
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml \
  exec -T -e ACTIVATED_AT_UTC="$activated_at" postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v activated_at="$ACTIVATED_AT_UTC" -P pager=off' <<'SQL'
WITH latest AS (
  SELECT id, run_id, scheduled_for
  FROM cycle_runs
  WHERE cycle_id = 2
    AND status = 'completed'
    AND scheduled_for >= :'activated_at'::timestamptz
  ORDER BY scheduled_for DESC
  LIMIT 3
)
SELECT
  latest.id AS cycle_run_id,
  latest.run_id AS agent_run_id,
  latest.scheduled_for,
  count(events.id) FILTER (
    WHERE events.event_type = 'run.tool_completed'
      AND events.payload->>'tool_name' = 'memory_reconstruct'
  ) AS reconstruct_calls,
  coalesce(jsonb_agg(events.payload->'args' ORDER BY events.sequence_no) FILTER (
    WHERE events.event_type = 'run.tool_completed'
      AND events.payload->>'tool_name' = 'memory_reconstruct'
  ), '[]'::jsonb) AS reconstruct_args,
  count(events.id) FILTER (
    WHERE events.event_type = 'run.tool_completed'
      AND events.payload->>'tool_name' = 'memory_ingest_source'
  ) AS ingest_calls,
  coalesce(jsonb_agg(events.payload->'args' ORDER BY events.sequence_no) FILTER (
    WHERE events.event_type = 'run.tool_completed'
      AND events.payload->>'tool_name' = 'memory_ingest_source'
  ), '[]'::jsonb) AS ingest_args
FROM latest
LEFT JOIN agent_run_events AS events ON events.run_id = latest.run_id
GROUP BY latest.id, latest.run_id, latest.scheduled_for
ORDER BY latest.scheduled_for DESC;
SQL
REMOTE
```

Acceptance requires exactly three rows. Every row must have
`reconstruct_calls >= 1`, and `reconstruct_args` must name concrete subjects
from that run (repo, issue/PR, person, chantier, or incident), not a generic
“what do you remember?” query. For a digest run that made a real durable
decision, `ingest_calls` must be exactly `1`; inspect `ingest_args` and require
content beginning `What future runs need:`, a durable decision/ownership/
guidance/conclusion, and the correct confidence (`0.9` human-stated,
`<= 0.7` inferred). Runs without a durable outcome should have zero ingests.
