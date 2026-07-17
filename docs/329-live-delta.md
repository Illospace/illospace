# Issue #356 activation — Domain 37 Uwear triage documents

This branch performs no live, SSH, deployment, or production database write.
After the code is deployed, Reda must run the activation command below against
the intended environment. The command replaces the manual #329/#344 record-id
runbooks; playbook ids come from the global database sequence and are never
predicted or pinned.

Deployment first runs forward-only migration
`0028_deactivate_pinned_chantier_digest`. Revision `0027_chantier_digest_v2`
is historical and remains byte-identical to `main`; `0028` removes its live
record-`1274` contract from coordinator cycle `2` and appends a safe revision.
It does not require doc `1155` v8 and never raises merely because activation is
pending. Missing prerequisites are logged and left unchanged, so migration
replay cannot hold a deployment behind this operator-run activation.

## Slug resolution contract

The bundled core document resolves each on-demand playbook in Domain `37` by
slug. It queries active `doc_page` records using the slug as search text,
requires exactly one result whose `data.slug` is an exact match, and only then
reads that returned record id in full. The bundled asset remains the explicit
fallback.

The activation command installs or verifies these mirrors:

| slug | bundled source | characters | bytes | SHA-256 |
|---|---|---:|---:|---|
| `uwear-engineering-triage-customer-support` | `references/customer-support.md` | `2599` | `2609` | `ccd9e6ded301f69fb493e6557c0f17312f05fdf8c8eba90f734101e73ebc9be8` |
| `uwear-engineering-triage-creating-work-items` | `references/creating-work-items.md` | `2576` | `2592` | `05acf69d6e473b64579eb84ace2bb995b923dc14c7887afe3cebde0f890b6c18` |
| `uwear-engineering-triage-backlog-maintenance` | `references/backlog-maintenance.md` | `2554` | `2558` | `1c2bc475b50a974f0b5efb5a41375c059cb13839e4b70c95d113f42faabe0abc` |
| `uwear-engineering-triage-chantier-operations` | `references/chantier-operations.md` | `11557` | `11565` | `61e78f92198db86916c5ebe753244fb862cb529a7295ec5a89df4d962efa9751` |
| `uwear-engineering-triage-memory` | `references/memory.md` | `4872` | `4898` | `fd42aad5b3af22c28982f3a49fb49d11606587ca0d7adb16cf37a63d8a0df503` |

Expected memory mirror fingerprint:

- characters: `4872`;
- bytes: `4898`; and
- SHA-256: `fd42aad5b3af22c28982f3a49fb49d11606587ca0d7adb16cf37a63d8a0df503`.

Core record `1155`, slug `uwear-engineering-triage`, must become the exact
mirror of `SKILL.md`:

- characters: `33943` (must remain `< 34000`);
- bytes: `34075`; and
- SHA-256: `6c318a9c20e2a8c3ce66fbacb2c0edf095d30a85e913c0caa855e6da452b0f9a`.

## Safety and idempotency

`brain.app.cli.activate_uwear_engineering_triage` runs in one transaction. On
PostgreSQL it briefly locks `domain_records` against concurrent inserts so the
global slug check and the following creates cannot race.

Before writing, it requires:

- record `1155` to be the active Domain `37` `doc_page` with slug
  `uwear-engineering-triage`;
- exactly one active Domain `37` `doc_page` object type;
- every existing target slug to resolve globally to at most one record, in
  Domain `37`, in that `doc_page` type, and in the core record's organization;
- core content that differs from the bundle to still be v7, so a newer live
  edit is never overwritten silently; and
- coordinator cycle `2` plus its revision history to be present and
  unambiguous; and
- every bundled source file to be readable.

A missing playbook is created with a database-assigned id. A correct existing
playbook is left untouched; stale content is replaced with the bundled bytes
using its locked current version. Core record `1155` is updated last and must
be v8 or newer afterward. Every record is then read back and byte-verified.
Only after that verification does the same transaction set cycle `2` and its
new latest revision to the slug-based v2 mission. If either half fails, both
the documents and mission roll back.

Any missing core target, occupied or duplicate slug, cross-domain/wrong-type
record, archived target, concurrent version change, unreadable asset, or
post-write mismatch raises an error and rolls back the entire transaction.
Once all six records match, another `--apply` changes no rows or versions.
It also leaves the already-correct mission and revision untouched.

## Production activation (Reda)

Run from the deployed checkout after the image containing this change is
available. This is the step that changes doc `1155` from live v7 to v8; merging
this code alone does not activate it. The normal deploy's Alembic step runs
`0028` before these commands, so the coordinator no longer points at unrelated
record `1274` while activation is pending.

```bash
cd ~/illospace

docker compose --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml \
  run --rm --no-deps api \
  python -m brain.app.cli.activate_uwear_engineering_triage --apply

docker compose --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml \
  run --rm --no-deps api \
  python -m brain.app.cli.activate_uwear_engineering_triage --apply

docker compose --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml \
  run --rm --no-deps api \
  python -m brain.app.cli.activate_uwear_engineering_triage --check
```

The first command reports the required creates/updates and
`mission_updated=true`. The second proves the write path is a no-op
(`created=0 updated=0 unchanged=6 mission_updated=false`). The final read-only
check must also report six unchanged documents and `mission_updated=false`.

Afterward, verify the live acceptance query separately and run one read-only
coordinator dry run. The query for record `1155` must return v8, `true`, and its
content must be byte-identical to the deployed `SKILL.md`. Each of the five
slugs above must resolve to exactly one active Domain `37` `doc_page` with the
listed fingerprint. The dry-run digest plan must be chantier-primary while its
footer still covers Reda, Axel, and JB. Coordinator cycle `2` and its latest
revision must contain slug `uwear-engineering-triage-chantier-operations` and
must not contain `record 1274`; historical revision `47` remains append-only
evidence of the superseded mission.
