> On-demand mode playbook for the Uwear engineering triage operating model.
> Core doc: Enterprise Documentation Domain `37` record `1155` (bundled skill
> `uwear-engineering-triage`); fetch per its **On-demand Run Modes** section.
> The core doc's always-on rules — Ownership, Deploy-State Ladder, States,
> Before Posting gates, Public Output — still govern this mode.

## Creating Work Items

Two different places can hold work, and they are NOT the same thing:

- A **real GitHub issue** on github.com, opened with the `create_github_issue`
  tool. This is a public write to a repo, with a real issue number and URL.
- An **internal coordination record** in the workspace tracker (Domain 1).
  Despite that domain being named "GitHub Ticket Tracker", a record in it is a
  private Illo database row — NOT a GitHub issue — and has no github.com URL
  unless one is filled in by hand.

Decide as follows.

### Filing floor — create the issue first

For every ticket-worthy customer-support report, **filing is unconditional**.
After the required investigation and duplicate check, choose the best-supported
repo from the available evidence and call `create_github_issue` immediately.
Create the issue before ownership resolution and before any routing question.
Uncertainty about the owner or exact route does not delay filing: state the
uncertainty in the issue body and keep the work visible.

Create the customer-support issue without `assignees`. Ownership and readiness
are post-create enrichment on an issue that already exists. Preserve any explicit
assignee request in the evidence so the post-create step can honor it.

### Post-create ownership and readiness enrichment

Immediately after `create_github_issue` returns the issue number and URL, apply
the core operating doc's **Ownership** section to the evidence collected for the
filed issue. That section is the single canonical resolver: do not copy its
people or work-class rules into this playbook.

- An explicit human assignment wins. Otherwise, resolve builder-first and use
  the core specialization and load-balancing tie-breakers. When ownership
  resolves, call `update_github_issue` with the resolved GitHub login in
  `assignees_add` during the same run.
- For a customer-generation report, use the investigation hypothesis as
  ownership evidence. "No owner up front" remains an investigation rule; it
  does not remove the mandatory post-create ownership attempt.
- Apply `ready-for-agent` with `update_github_issue` when the filed issue meets
  the definition in the core **States** section: it is scoped enough for an
  autonomous agent to implement and open a PR without human judgment,
  credentials, external testing, or a manual design/release decision.
- **Mandatory branch — I don't know the owner.** This branch requires no owner
  to be known and never blocks filing; the issue already exists. Keep it
  unassigned, add `Ownership: Unassigned — <specific ambiguity or missing
  evidence>.` to the issue body with `update_github_issue`, and state exactly
  what ownership or routing fact remains unresolved.
- A routing question is never a substitute for filing. Ask it only after the
  issue exists. Register it as an open ask with a named human owner and an
  explicit expiry so the terminal-state machinery tracks it, then @-mention
  that human in Slack and cite the filed issue. Do not let a newly opened
  routing question carry `answers_open_ask: false` unless its `open_asks` row
  was registered separately.

### Customer-bug filing policy

- **Customer-reported bugs have one declared destination.** The durable artifact
  is a real GitHub issue in the owning repo; Domain `1` is its linked tracker
  mirror, never a substitute. Create the GitHub issue first, then mirror it with
  the returned issue number/URL and stable external id. The issue body must carry
  the customer's own words, the concrete impact (including credit loss), and the
  Slack `origin_ref`; follow the filing floor and post-create enrichment above,
  including honoring an explicit assignee request using the verified GitHub
  identity after the issue exists. If no more-specific tracker exists, use Domain
  `1` as the existing default. Never create a Domain during filing: propose the
  schema change for later review and complete the immediate mirror in Domain `1`.
- **One problem = one issue — check before filing.** Before calling
  `create_github_issue`, search open AND closed GitHub issues and Domain 1
  tracker records for the same tracked error signature, Rollbar id (prefer
  the structured `rollbar_item` field), endpoint, profile id, or root cause.
  A Rollbar `New error:` title with a different tracked signature is a
  different failure mode even when a related parent issue exists: file it or
  add an explicit new-signature entry to that parent; never silently absorb
  it based only on item id or similar timeout vocabulary. An exact tracked
  signature match has no recency cutoff. If a match
  exists — even if closed or `Done` — do NOT file a new issue and do NOT
  blindly skip: follow the **Deploy-State Ladder** in the core operating doc
  (record `1155`). Never split one error/Rollbar alert into multiple issues.
- **Repo and incident are both clear and a write-capable token can reach the
  repo:** open a real GitHub issue with `create_github_issue` in the correct
  repo (`uwear-ai/uwearaiapp`, `uwear-ai/uwear-backend`,
  `uwear-ai/uwear-mobile-app`, or `uwear-ai/uwear-website`). Prefix an
  AI-authored body with `> *This was generated by AI during triage.*` Report it
  with the returned issue number and URL.
- **`create_github_issue` returns an error** (for example `no_write_token`, or a
  403/404): do NOT claim an issue was filed. A retention tracker record + handoff
  may keep the work from being lost, but the reply must say **the requested GitHub
  issue was not created** and name the exact blocker.
- **Repo or incident remains uncertain after investigation:** for a ticket-worthy
  customer-support report, choose the best-supported repo, file there, and state
  the unresolved routing fact in the issue body. Ask a routing question only
  after filing and follow the tracked open-ask requirements above. For other
  intake, capture an internal record if the signal must not be lost.

Never describe an internal tracker record as a GitHub issue. Only say a GitHub
issue was opened when `create_github_issue` succeeded and you can cite its number
and URL. When the user names an artifact type (`ticket` or `issue`), produce that
artifact or explicitly name the artifact and its blocker; never silently report a
different artifact as success.

Tracker records use stable external ids: `github:<owner>/<repo>:issue:<number>`,
`github:<owner>/<repo>:pr:<number>`, or `coordination:<repo>:<stable-task-key>`.
