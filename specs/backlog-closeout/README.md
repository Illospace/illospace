# Illospace backlog closeout

Goal: review every issue open on 2026-10-07, resolve each source defect or complete its operational acceptance, and offer one PR for the user to merge before redeploy. Do not claim deployment acceptance from a passing unit test.

## Next Agent Prompt

Work in `/Users/redamjahed/.codex/worktrees/retire-staging-routine/illospace-project`, branch `codex/retire-staging-routine`, PR #930. The dirty primary checkout belongs to other work and must remain untouched. Imported #910, #911 and #921; joined Reflex migration after the main usage migration at 0068. Continue independent slices below, integrate their focused commits, run full gates and review. Do not merge or deploy: the user will merge the single final PR and then redeploy. Update this section at each checkpoint.

- [x] Import existing PR work and retire staging routine.
- [ ] Inbound metadata and size integrity (#770, #897); agent backlog_plan_a.
- [ ] Provider credential circuit (#869); agent backlog_plan_b.
- [ ] Cue extraction and measured recall decision (#670, #723); agent backlog_plan_c plus root live measurement.
- [ ] Event drain, Cycle authority, scheduler diagnostic cause (#926, #928, #929); root.
- [ ] Verify merged fixes (#903, #908, #914-#919) through consumers and historical evidence.
- [ ] Tracker reconciliation and backfill (#900, #905, #920).
- [ ] Skill and routine migration (#817, #819, #821, #822).
- [ ] Resolve connectivity reports with evidence (#829, #874, #907).
- [ ] Full backend, database, migration, context admission gates and final code review.
- [ ] One rewritten ready PR, complete ticket evidence ledger, close only completed/retired issues and superseded PRs.

## Contracts

One owner per input envelope, memory eligibility, credential health transition, Cycle occurrence provenance, event persistence, and tracker identity. Reuse existing services. Do not add alternate flag paths or duplicate rules. Original memory first-sentence reuse is intentional under #916's corrected acceptance; retain the disclosed reuse result. One inherited Cycle occurrence must share the notification ledger and throttle with every continuation. No authority from model text. No raw customer data, private transcripts or secrets enter the public tree.

## Slice graph

Imported fixes precede all source slices. Inbound, credential, cues, and root runtime patches are independent. Source fixes converge on integration tests; runtime repair follows supported live services. All evidence converges on one PR. Source changes requiring deployment remain pending verification; do not use closing keywords to hide incomplete runtime requirements.

## Evidence ledger

26 open issues at start: #670 #723 #770 #817 #819 #821 #822 #829 #869 #874 #897 #900 #903 #905 #907 #908 #914 #915 #916 #917 #918 #919 #920 #926 #928 #929. Final per-ticket evidence lives in `docs/backlog-closeout-2026-10-07.md`. Worktree-local test/review logs belong outside the repository; durable aggregate results belong in that report.
