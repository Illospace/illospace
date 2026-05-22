# Project Workspaces

Projects give agents an unambiguous workspace. A Project may start empty as a
named collaboration container, then gain files, folders, repositories, and
documents over time. Once resources exist, agents see them through stable
Project mount paths.

This page documents the backend contract for Project roots and thread drafts.

## Domain Model

- Project root: the published source of truth. Users and future threads read
  from the root.
- Empty Project: a valid Project with no resources yet. It carries identity,
  visibility, sharing, and future workspace intent, but has no mount paths until
  resources are added.
- Project resource: one mounted file, folder, repository, or external resource
  in a Project.
- Project mount path: the agent-facing truth for a resource location, such as
  `/reports` or `/repos/backend`.
- Thread draft: the writable workspace for one thread. Agents edit the draft,
  not the root.
- Base: a hidden per-draft snapshot of root content from the moment the draft
  path was last refreshed or published. Base exists for three-way comparison
  and intent recovery; it is not a user-facing workspace.
- Draft resource: the per-resource implementation behind the thread draft.
  Local files use Project draft metadata and base snapshots. Repositories use
  Git status, branches, upstream checks, and PR publishing.

## Invariants

- The Project root is read-only during normal agent work.
- A Project can have zero resources. Empty Projects are valid and report a
  clean draft state with no mount paths.
- The thread draft is the only writable local workspace for Project-mounted
  paths.
- `draft_status` and `plan_publish` are read-only.
- `refresh_draft_from_root` is the explicit action that copies latest root
  changes into untouched draft paths.
- `publish_draft` is the only local-file action that writes draft changes back
  to Project root.
- Published root history is durable and is not deleted by draft cleanup.
- Archived clean drafts can be deleted immediately. Archived unpublished drafts
  are retained for a grace period and then cleaned by the scheduled draft
  cleanup job.

## Local File Workflow

1. Materialization creates a thread draft and records a base manifest.
2. Agents read and write through Project mount paths, which resolve to the
   thread draft.
3. `draft_status` compares base, latest root, and draft without mutating files.
4. `plan_publish` builds grouped operations and capped diff previews:
   `root -> draft` shows what would publish, and `base -> draft` shows thread
   intent.
5. If root changed and draft did not, status is `out_of_date`.
6. If root and draft changed the same path differently, status is `conflicted`.
7. `refresh_draft_from_root` updates untouched files from root and preserves
   draft edits.
8. `publish_draft` captures a before-version, applies non-conflicting draft
   changes, captures an after-version, and updates base metadata for published
   paths.
9. If publish fails, the root is restored from the before-version.

## Conflict Resolution

Conflicts are resolved by the agent, not by a deterministic merge algorithm.
When `publish_draft` sees conflicted paths, it returns
`project_draft_conflicts_require_resolution` with:

- conflicted root and draft paths;
- `root -> draft` diff previews;
- `base -> draft` diff previews;
- instructions to preserve user intent while respecting the current root;
- the retry action: `manage_project(action="publish_draft")`.

The first blocked publish records a conflict checkpoint with the current root
and draft entries. On retry, if root is still at that checkpoint, the draft can
be treated as the agent's explicit resolution and published. If root changed
again, publish blocks again and refreshes the conflict checkpoint.

## Repository Resources

A Project can contain repositories, but Projects are not fundamentally Git
repositories. Repo resources use per-resource draft implementations:

- status comes from Git working tree status;
- upstream freshness checks compare draft paths against the latest base branch;
- publish can create commits, branches, pushes, and PRs;
- overlapping upstream changes block publish like local root conflicts.

The user does not need to think of a Project as Git-backed. Git is only an
implementation primitive for repository resources.

## Backend Modules

- `workspace_manifest.py`: canonical Project mount paths and resource mounts.
- `materializer.py`: creates or refreshes thread draft workspaces.
- `drafts.py`: local file manifests, base snapshots, root-to-draft refresh, and
  publish planning.
- `draft_conflicts.py`: checkpoint predicates and normalization.
- `draft_diff.py`: capped `root -> draft` and `base -> draft` previews.
- `draft_state.py`: `draft_status` and `refresh_draft_from_root` payloads.
- `publish.py`: publish plans, conflict guidance, local root publish, and repo
  publish orchestration.
- `versioning.py` and `root_history.py`: root version capture, preview, and
  restore.
- `draft_lifecycle.py`: archived-thread draft cleanup and retention.
