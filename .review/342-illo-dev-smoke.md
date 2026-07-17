# Issue #342 illo-dev smoke gate

Do not run this in the sandboxed worktree. Run it on illo-dev after deployment,
with the app environment loaded and a `psql`-compatible database URL available.

```bash
export ILLO_DB_URL="${DATABASE_URL/postgresql+asyncpg/postgresql}"
export ILLO_DB_URL="${ILLO_DB_URL/postgresql+psycopg/postgresql}"
export ILLO_USER_ID="<reda-user-uuid>"
export ILLO_ORG_ID="<uwear-org-uuid>"

python3 -m brain.app.cli.memory backfill-embeddings --batch-size 25

for reconstruction_id in 70 71 72; do
  query="$(psql "$ILLO_DB_URL" -Atc "SELECT query_text FROM reconstruction_runs WHERE id = ${reconstruction_id}")"
  echo "reconstruction ${reconstruction_id}: ${query}"
  python3 -m brain.app.cli.memory query "$query" --limit 5 --user-id "$ILLO_USER_ID" --org-id "$ILLO_ORG_ID" | jq -r '.results[] | [.id, .scores.confidence, .content] | @tsv'
done

python3 -m brain.app.cli.memory query "axel-havard" --limit 5 --user-id "$ILLO_USER_ID" --org-id "$ILLO_ORG_ID" | jq -r '.results[] | [.id, .scores.confidence, .content] | @tsv'

psql "$ILLO_DB_URL" -Atc "
SELECT count(*)
FROM memory_nodes AS node
WHERE node.node_kind IN ('content', 'summary', 'procedure', 'policy')
  AND NOT EXISTS (
    SELECT 1
    FROM memory_node_embeddings AS embedding
    WHERE embedding.node_id = node.id
      AND embedding.embedding_kind = 'content'
      AND embedding.embedding IS NOT NULL
  );"
```

Pass conditions:

- Each run 70–72 query lists Aritzia evidence above the staging-deploy receipt.
- The exact `axel-havard` lookup returns the node containing that handle.
- The final count is `0`. Re-running the backfill reports only skipped vectors.

If the backfill is interrupted, rerun the same command; committed vectors are
skipped. For deliberately chunked runs, combine `--limit` with
`--after-id <last_node_id>` from the prior JSON output. If `failed` is nonzero,
rerun without `--after-id` so gaps are retried. Cue and tag nodes are intentionally
excluded because recall ranks source-backed content-bearing nodes; embedding
terse routing vocabulary would add cost and crowd semantic candidates.
