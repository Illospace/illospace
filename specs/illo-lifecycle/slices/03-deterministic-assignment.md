# Slice 3 — Deterministic assignment

**Depends on Slice 2 (`task_type`).** Fix the wrong-owner fumble structurally:
typed rules for high-stakes routes; everything genuinely unowned goes to a
visible **unclaimed pool** teammates pull from — never auto-pushed onto a person,
never silently skipped.

## Contract unlocked
A single owner-resolution seam decides who owns an item, keyed on
`task_type`/repo/policy. Business/PM → Reda is a hard rule. Items with no rule and
no connection owner are parked, ownerless, in a visible unclaimed pool (Reda's
"fourth list") and surfaced for organic pickup. No auto-assignment; no silent
skip.

## API seam / changes
1. **Resolve-owner function (one owner)** — new seam, e.g.
   `brain/systems/inbound/assignment.py`:
   `def resolve_owner(item, task_type, repo, policy) -> OwnerDecision`
   where `OwnerDecision = (user_id | None, basis)` and `basis ∈ {rule,
   connection, unassigned, judgment}`. Deterministic and unit-testable. Ordered:
   1. **typed rule** (e.g. `business|product → Reda`; repo→owner table);
   2. **connection authority** (`owner_user_id`, service.py:189);
   3. **unassigned** — return no owner; the item is parked in the unclaimed pool.
      This replaces both the silent skip (service.py:867,1096) and the earlier
      auto-assign idea. Nothing is dropped, nothing is force-pushed to a person.
   4. **judgment** (prose) only when a rule intentionally defers.
2. **Unclaimed pool = a state, not a new store** — Ideas/domain records with no
   owner + an `open`/`unclaimed` status. It's a query over existing tables (keeps
   README invariant 1 — no parallel store). Triage _parks_ the item (create the
   Idea with `user_id=null` + unclaimed marker) instead of skipping. Surface it as
   a distinct list alongside the team's existing views (the "fourth list"); map to
   however the team already sees lists.
3. **Claim / pick-up** — a teammate self-assigns via the existing `manage_idea`
   `user_id` mechanism (tool_definitions.py:1172–1174,1259): setting
   `user_id=self` moves it out of the pool. Confirm any teammate can claim an
   unowned item; add a light "claim" affordance if `manage_idea` alone isn't
   ergonomic.
4. **Wire into triage** — the triage queue path
   ([service.py](../../../brain/systems/inbound/service.py):857–959) calls
   `resolve_owner`; sets the Idea/thread `user_id` when resolved, else parks it
   unclaimed. Connection authority and SOUL/policy prose are fallbacks _behind_
   the rule — not parallel deciders (README invariant 3).
5. **Preserve "1155" intent (A3)** — at build time, read the current business/PM
   policy `instructions` and SOUL `## Coordination` prose; migrate the
   _deterministic_ parts (known repo→person routes, don't-guess-unknowns) into
   the rule table, and leave only genuine judgment as prose. Do not silently drop
   any current routing behavior.
6. **Retire Slice 0's soft route** — once the typed rule for business/PM → Reda
   is live, remove the transitional policy `instruction` from Slice 0.
7. **Author-nudge norm** — keep the SOUL norm from Slice 0; ensure `resolve_owner`
   never assigns a reviewer/coordination-owner for a PR (routes to nudging the
   author instead).
8. **Visibility (cross-ref Slice 4)** — the pool only works if pickup actually
   happens: Slice 4's digest names unclaimed items, and `illo_read`/`domain.inspect`
   can filter to unclaimed.

## What the human can run/see
- Unit probe: `resolve_owner(business_item)` → `(Reda, rule)`;
  `resolve_owner(unknown_repo_eng_item, no connection owner)` →
  `(None, unassigned)` — parked, not guessed, not force-assigned.
- A business/PM issue through triage → owned by Reda with `basis=rule`.
- An item with no rule + no connection owner → appears in the unclaimed pool; a
  teammate claims it and it leaves the pool.

## Verification
- Rule table drives ownership for business/product; fixtures cover each
  `task_type`.
- The silent-skip path is gone: a no-owner item lands in the unclaimed pool
  (queryable), never dropped, never auto-assigned.
- Claim: setting `user_id=self` removes it from the pool exactly once.
- Regression: prior known routes (from "1155") still produce the same owners.

## Must stay green
- Engineering triage ownership unchanged for the cases "1155" already handled.
- No reviewer/coordination-owner is ever auto-assigned on a PR.

## Feedback that would change this slice
- How aggressively the digest nudges unclaimed items (Slice 4), and any staleness
  escalation (e.g. an item unclaimed for N days gets flagged).
- The rule table's shape (config vs code vs policy-backed) and who besides Reda is
  a default owner for `ops`/`product`.
