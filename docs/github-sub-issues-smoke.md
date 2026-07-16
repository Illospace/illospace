# GitHub sub-issue live smoke recipe

This is a manual, non-CI smoke test for Illo's live GitHub App write path. Use
temporary issues in `Illospace/illospace`, then close them when the check is
complete.

## Prerequisites

- The Illo GitHub App is installed on `Illospace/illospace` with **Issues: Read
  and write**.
- Its Vault credential is project-bound to `Illospace/illospace`, so the tools
  report `token_source: project_binding:GITHUB_TOKEN` (or the configured App
  env alias).

## Recipe

1. Call `create_github_issue` twice in `Illospace/illospace`:

   - Parent title: `[Chantier] Sub-issue smoke <date>`
   - Parent body: include `Done means the native add/list/parent/remove round
     trip succeeds.`, a scratch chantier slug, and a reference to issue #328.
     Do not add a Markdown child checklist.
   - Child title: `[Scratch] Sub-issue member <date>`

2. Call `list_github_sub_issues` with `action: "list"`, the parent issue
   number, and `repo: "Illospace/illospace"`. Expect an empty `sub_issues`
   array.

3. Call `add_github_sub_issue` with both repos set to
   `Illospace/illospace` and the two issue numbers. Expect `action: "linked"`
   and `changed: true`.

4. Repeat the same add call. Expect `action: "already_linked"`,
   `already_linked: true`, and `changed: false`.

5. List the parent again. Expect the child number and URL in `sub_issues`, and
   confirm GitHub's issue page renders its native progress rollup.

6. Call `list_github_sub_issues` with `action: "get_parent"`, the child issue
   number, and `repo: "Illospace/illospace"`. Expect the scratch parent in
   `parent`.

7. Call `remove_github_sub_issue` with the same refs. Expect
   `action: "unlinked"` and `changed: true`. Repeat it once and expect
   `action: "already_unlinked"` with `changed: false`.

8. List the parent once more and verify the child is absent. Close both scratch
   issues and keep their URLs in the deployment verification note.

Any HTTP 403 must be treated as a failed smoke: reapprove or reconnect the App
with **Issues: Read and write** before retrying. Do not record the relationship
as created when a tool returns `no_write_token` or `missing_scope`.
