# Search, archive import, and notes

Search uses Postgres full text. It reads items and the last 20 events of each lane.
It merges each source's ordered results by reciprocal rank. It removes duplicate source refs.
A small recency boost breaks close item scores. It uses no vectors.
Private items and lanes appear only for their active owner. A private result sets `personal_read` on the run.
Tainted and outsider runs cannot use these tools.
Slack items come only from configured public channels.

`search_all(query, kinds?, limit?)` returns compact text. The limit is 1–50; the default is 10.
`search_get(source_ref)` returns a full item, a lane with its events, or a GitHub issue.
Lane refs use `lane:<ledger id>`. Live issue refs use `github:<owner>/<repo>#<number>`.
GitHub search uses only the configured repositories. Queries need at least three letters or digits.
Each live source has a four-second limit. Failed sources leave partial results with a notice.

## Add a search provider

Call `illo.sources.search.register_provider(name, fn, personal=False)` from the provider's setup hook.
The async function receives `(ctx, principal, query, kinds, limit)`.
It returns an ordered list of `Hit` objects from `illo.sources.search`.
A source ref must name the same source object on every call.

A personal provider resolves its grant from the authenticated principal.
It never accepts an owner from the query.
Register it with `personal=True`. Only an untainted `person_dm` run of the owner uses it.
Its hits set `personal_read`, even when the final limit drops those hits.
Private ledger hits set that flag too.

## Import an archive

Run migrations first. Set `ILLO_DATABASE_URL` and `ILLO_WORKSPACE_DIR`.
Then run:

```sh
illo import-archive archive.jsonl --dry-run
illo import-archive archive.jsonl
```

Each line holds one JSON object. Supported keys:

- `source_ref`, `title`, `text`, and `summary`.
- `created_at` and `updated_at`: ISO dates with times.
- `owner`: an active person key, an email address, or null.
- `visibility`: `team` or `private`.
- `provenance`: an object. `links` and `aliases`: lists.

A ref without a prefix gets `archive:`. Its old ref still resolves as an alias.
Aliases pass the same visibility check as the canonical ref.
An alias cannot replace another item's ref. An archive cannot replace another item kind.
A private entry without an active owner is skipped. Its visibility stays private.
The command prints loaded, updated, and skipped counts with reasons.
A dry run changes no rows. Repeated imports update the same item.

## Keep a note

`notes_search(query)` reads confirmed team notes.
`notes_propose(title, text, about)` sends the requester a DM card.
The card says `I noted: <text>` and has Keep, Correct…, and Remove buttons.
Keep writes the file. Correct… requests the replacement text in that DM thread.
The owner's next reply in that thread writes the corrected file. Remove deletes the proposal.
A button must belong to that owner and that card in the DM.

A team or tainted run cannot propose a note.
A run that reads personal data first creates a draft for the requester.
Draft approval permits a proposal; Keep still confirms the team note.
A note always has team visibility.

Notes live in `<workspace>/notes/*.md`. Their front matter has:

```yaml
---
title: Release decision
about: Release process
author: ada
created: '2026-01-01T09:00:00+00:00'
visibility: team
---
```

The Markdown body follows the front matter. The folder is the truth.
Illo syncs the item index at start, after a confirmation, and on the workspace timer.
Removed files leave the index. Plain Markdown notes from older workspaces still load.
New notes use front matter. Note files and the notes folder must not be symbolic links.

To commit and push confirmed notes, add this to `illo.yaml`:

```yaml
workspace:
  git_push: true
```

The default is false. The workspace needs a Git checkout and a remote.
The commit contains only the note. A failed commit or push leaves the file in place.
The `workspace` sensor reports the failure. Git credentials stay outside the workspace.
