# Issue #329 deploy note — Domain 37 live delta

This branch intentionally performs no live/SSH write. Deployment must apply
the following exact Domain `37` changes after the commit lands.

## 1. Create the on-demand chantier operations record

Create a `doc_page` with:

- intended record id: `1274` (the core pointer is pinned to this id; assert the
  returned id before continuing);
- slug: `uwear-engineering-triage-chantier-operations`;
- title: `Uwear Engineering Triage — Chantier Operations`; and
- content: the exact UTF-8 contents of
  `brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/references/chantier-operations.md`.

Expected content fingerprint:

- characters: `8352`;
- bytes: `8358`; and
- SHA-256: `e98123651c41fe935918bf79b0a5598bc7831ad73a91c7ee73442d7a8f28e006`.

After creation, read record `1274` back and require byte identity with the
bundled reference. If id `1274` is already occupied or the content differs,
stop deployment: do not update core record `1155` to point at a missing or
different playbook.

## 2. Replace core record 1155 with doc v8

Read Enterprise Documentation Domain `37`, `doc_page` record `1155`, and
verify slug `uwear-engineering-triage`. It is expected to be v7 immediately
before this activation. Apply `manage_domain` `action=update_record` by
`record_id=1155` with the just-read `expected_version`, preserving all record
fields except:

- set `data.content` to the exact UTF-8 contents of
  `brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/SKILL.md`.

This is a whole-content replacement, not a hand-edited approximation. The
target v8 content adds exactly these operating-model changes:

- the record-1274 fetch trigger and always-on chantier invariants;
- active-chantier coverage and freshness in Phase A;
- chantier state/member/blocker data in the Phase-B snapshot, including
  no-silent-departure;
- chantier-primary digest v2: exact chantier count, moved-chantier sections,
  quiet roll-up, `Loose items`, and the mandatory Reda/Axel/JB recap footer;
- attach-at-triage/induction invariants before new-work filing; and
- freshness and goal-language close-out gates in Before Posting.

Expected v8 fingerprint:

- characters: `33834` (must remain `< 34000`);
- bytes: `33972`; and
- SHA-256: `1a98c7eb2b4112e7fed5a70e34554616cc4afcabf40c4343cd1e14ee788467bc`.

If record `1155` is no longer v7, re-read and compare before writing. Do not
silently overwrite a later live edit; reconcile it into the bundle first so
the final record and bundled `SKILL.md` remain byte-identical.

## 3. Verification

After both writes:

1. Read records `1155` and `1274` back in full.
2. Confirm record `1155` is v8 and its content exactly matches bundled
   `SKILL.md` by byte count and SHA-256.
3. Confirm record `1274` exactly matches bundled
   `references/chantier-operations.md` by byte count and SHA-256.
4. Confirm the core pointer names record `1274` and the matching asset path.
5. Run one read-only coordinator dry run and verify the digest plan is
   chantier-primary while the footer still covers Reda, Axel, and JB.
