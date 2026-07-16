# Domain 1 Chantier Record Contract

This document defines only the persisted schema for `chantier` records in the
`github-ticket-tracker` Domain. Chantier operating behavior belongs in its
separate behavior document.

## Object type

- Domain slug: `github-ticket-tracker` (production Domain id `1`)
- Object key: `chantier`
- Display name: `Chantier`
- Record title field: `title`
- Contract version marker: `chantier-record-contract-v1`

The dormant `milestone` object is unrelated and is not an alias or predecessor
for this type.

All fields below live in the record's `data` object for `manage_domain`
creates, updates, and query results.

## Fields

| Field | Type | Required | Contract |
| --- | --- | --- | --- |
| `slug` | text | yes | Stable after creation; lowercase kebab-case, maximum 80 characters. |
| `title` | text | yes | Human-readable chantier title, maximum 500 characters. |
| `goal` | long text | yes | Outcome hook beginning with `Done means …`, maximum 4,000 characters. |
| `kind` | enum | yes | `feature`, `incident`, `quality`, or `gtm`. |
| `state` | enum | yes | `exploring`, `building`, `shipping`, `verifying`, `done`, or `paused`. This chantier state composes with, and does not replace, member tickets' Deploy-State Ladder. |
| `owner` | text | no | Next-action owner, using the ticket record's builder-first ownership semantics; maximum 120 characters. |
| `refs` | JSON array | yes | Typed references with the item shape below. An empty array is valid. |
| `parent_issue` | text or null | no | GitHub mirror parent issue in external-id format. |
| `next_step` | text | yes | One non-empty line naming the next most valuable step; maximum 500 characters. |
| `progress_note` | long text | no | Progress context using the ticket record convention; maximum 2,000 characters. |
| `created_at` | datetime or null | no | ISO 8601 source timestamp, following existing Domain-1 timestamp conventions. |
| `updated_at` | datetime or null | no | ISO 8601 source timestamp, following existing Domain-1 timestamp conventions. |

Each `refs` item is an object with exactly these keys:

- `source` (required): `github`, `doc`, `slack`, `posthog`, or `url`
- `ref` (required): a non-empty string
- `title` (optional): a non-empty display string

When `source` is `github`, `ref` must use
`github:<owner>/<repo>:issue:<n>`. `parent_issue` uses that same format. The
repository segment is part of the reference, so chantier membership is
cross-repository by construction.

## Full example record

```json
{
  "slug": "agent-runtime-chantier-layer",
  "title": "Agent runtime chantier layer",
  "goal": "Done means related work is coordinated as one outcome across every affected repository.",
  "kind": "feature",
  "state": "building",
  "owner": "Reda",
  "refs": [
    {
      "source": "github",
      "ref": "github:Illospace/illospace:issue:326",
      "title": "Chantier layer umbrella"
    },
    {
      "source": "github",
      "ref": "github:Illospace/illospace:issue:327",
      "title": "Domain-1 chantier object type and record contract"
    },
    {
      "source": "doc",
      "ref": "brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/references/chantier-record-contract.md",
      "title": "Chantier record contract"
    }
  ],
  "parent_issue": "github:Illospace/illospace:issue:326",
  "next_step": "Merge the object-schema migration after its Domain and Alembic checks pass.",
  "progress_note": "The keystone schema is implemented and awaiting verification.",
  "created_at": "2026-07-16T14:00:00Z",
  "updated_at": "2026-07-16T16:00:00Z"
}
```
