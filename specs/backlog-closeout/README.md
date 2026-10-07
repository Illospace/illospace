# Illospace backlog closeout

Goal: review every issue open on 2026-10-07, resolve each source defect or complete its operational acceptance, and offer one PR for the user to merge before redeploy. Do not claim deployment acceptance from a passing unit test.

## Next Agent Prompt

Source work is complete on `codex/retire-staging-routine`, PR #930. The six earlier PR heads are included and closed as superseded. Integrated source `a19a87495cc5a80aa71bc1d4d776b6b3d1810446` passed 5,849 fast tests, 95 PostgreSQL tests, clean migrations through 0069 and final source review. The user will merge and redeploy. Then use `docs/github-tracker-recovery.md` to add the missing projections, repair duplicates and reconcile fresh source state. Verify each remaining ticket's original symptom before closing it. Eight decisions/recovered incidents are closed; 18 deployment acceptance tickets remain open. Preserve the dirty primary checkout. Never repair a production memory node by assuming that its number matches the historical dev number.

- [x] Import existing PR work and retire staging routine.
- [x] Inbound metadata and size integrity (#770, #897); agent backlog_plan_a.
- [x] Provider credential circuit (#869); agent backlog_plan_b, integrated and verified.
- [x] Cue extraction and measured recall decision (#670, #723); agent backlog_plan_c plus root live measurement.
- [x] Event drain, Cycle authority, scheduler diagnostic cause (#926, #928, #929); root.
- [x] Verify merged fixes (#903, #908, #914-#919) through consumers and historical evidence.
- [x] Tracker reconciliation and backfill source (#900, #905, #920); live acceptance follows deploy.
- [x] Retire old routine migration (#817, #819, #821, #822), per user decision.
- [x] Close recovered connectivity reports (#874, #907) and retire public addresses (#829), per user decision.
- [x] Full backend, database, migration, context admission gates and final code review.
- [x] Single PR and complete ticket evidence ledger; close only completed/retired issues and superseded PRs.

## Contracts

One owner per input envelope, memory eligibility, credential health transition, Cycle occurrence provenance, event persistence, and tracker identity. Reuse existing services. Do not add alternate flag paths or duplicate rules. Original memory first-sentence reuse is intentional under #916's corrected acceptance; retain the disclosed reuse result. One inherited Cycle occurrence must share the notification ledger and throttle with every continuation. No authority from model text. No raw customer data, private transcripts or secrets enter the public tree.

## Slice graph

Imported fixes precede all source slices. Inbound, credential, cues, and root runtime patches are independent. Source fixes converge on integration tests; runtime repair follows supported live services. All evidence converges on one PR. Source changes requiring deployment remain pending verification; do not use closing keywords to hide incomplete runtime requirements.

## Evidence ledger

26 open issues at start: #670 #723 #770 #817 #819 #821 #822 #829 #869 #874 #897 #900 #903 #905 #907 #908 #914 #915 #916 #917 #918 #919 #920 #926 #928 #929. Final per-ticket evidence lives in `docs/backlog-closeout-2026-10-07.md`. Worktree-local test/review logs belong outside the repository; durable aggregate results belong in that report.
