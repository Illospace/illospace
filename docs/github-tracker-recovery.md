# Recover GitHub tracker rows

Recovery uses a fresh GitHub snapshot and one explicitly selected, enabled
tracker projection. It does not replay old webhook decisions. The normal
`manage_inbound.replay_events` command is a preview; repeating an existing
webhook idempotency key returns its original receipt.

The October 2026 audit found the GitHub Events feed projection and no tracker
projection. A code deploy alone does not add the missing configuration or
recover old records. From the deployed checkout, with its normal database
configuration, preview the additive setup, then apply the reviewed result:

```bash
python -m brain.app.cli.configure_github_tracker --org-id "$tracker_org_id"
python -m brain.app.cli.configure_github_tracker --org-id "$tracker_org_id" --apply
```

The command resolves the org's existing `github_event` feed projection and its
connection/policy, then the `github-ticket-tracker` Domain. It uses canonical
`admin.create_projection` to add missing ticket and PR projections. It preserves
the feed and refuses to replace conflicting tracker configuration. A repeat
reuses matching projections. Keep the returned IDs for the recovery commands.

`metadata.when` uses the shared mapping condition language: issues go to tickets,
PRs go to PR records, and comments continue to the existing feed. A false
condition has an explicit skipped receipt; invalid conditions fail configuration
validation. Ticket closure sets `github_state=closed` and source timestamps.
Source observations preserve editorial status and the next-action owner. Triage
and deploy reconciliation own Done and release evidence; a closed GitHub snapshot
cannot replace `In Review` while production is pending. Canonical new
ticket creation uses the schema status default, or Backlog when none exists.
PR author comes from the original PR subject, and PR state follows GitHub.

Run recovery separately for each selected tracker projection. Schema,
condition, source-policy, and identity checks prevent applying an issue snapshot
through a PR projection or a different organization.

Fetch the current subject JSON from GitHub's issues endpoint for issues, or
the pulls endpoint for PRs. Build a local JSON file in this format:

```json
{
  "captured_at": "2026-10-07T16:00:00Z",
  "items": [
    {
      "event": "issues",
      "repository": "owner/repository",
      "subject": {"number": 123, "html_url": "https://github.com/owner/repository/issues/123", "title": "Current title", "state": "closed", "updated_at": "2026-10-07T15:00:00Z", "user": {"login": "original-author"}}
    }
  ]
}
```

Use the complete GitHub API response for `subject`, including all fields the
projection maps. Set `captured_at` after fetching all selected items. A PR
subject must include its `merged` boolean. The original subject's `user.login`
is required; `hints.author` maps that author. Comment snapshots are rejected.
Keep private payloads in the local
file; the recovery output contains identities, field names, and record versions.

For duplicate-only repair, first run:

```bash
python -m brain.app.cli.recover_tracker --org-id "$tracker_org_id" --projection-id "$tracker_projection_id" --snapshot "$tracker_snapshot_path" --deduplicate-only > tracker-duplicates-plan.json
python -m brain.app.cli.recover_tracker --org-id "$tracker_org_id" --projection-id "$tracker_projection_id" --snapshot "$tracker_snapshot_path" --deduplicate-only --apply --plan tracker-duplicates-plan.json
```

This observes the canonical row's own complete data and title, archives copies,
and skips missing identities. It preserves canonical editorial state. The mode
is bound to the reviewed plan; applying that plan in normal mode is rejected.

Then reconcile current GitHub state and backfill missing rows with a separate
normal plan:

```bash
python -m brain.app.cli.recover_tracker --org-id "$tracker_org_id" --projection-id "$tracker_projection_id" --snapshot "$tracker_snapshot_path" > tracker-plan.json
python -m brain.app.cli.recover_tracker --org-id "$tracker_org_id" --projection-id "$tracker_projection_id" --snapshot "$tracker_snapshot_path" --apply --plan tracker-plan.json
```

Review the plan and the source snapshot before applying it. An apply rejects
changed snapshots, projection schemas, record versions, and older mapped
freshness timestamps. All items validate before writes, and the command uses
one transaction. Existing rows retain their earliest canonical record ID;
duplicate copies are archived through the normal Domain service. Missing rows
are created. Supplied source fields update through the configured projection,
while fields outside that projection remain under their current owner.

An identical completed recovery is a no-op. Selected projection keys that
pointed to duplicate rows are repaired. Old inbound events and their receipts
remain intact. Recovery starts no Illo runs and changes no source policies,
projections, or other Domains. Confirm one active canonical row per identity,
then confirm a new normal webhook updates the selected tracker and the existing
GitHub Events Domain.
