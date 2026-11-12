---
name: release-check
description: Say if a release of acme-example/engine is ready, and what blocks it.
---
Use this skill for "can we release?" or "is the release ready?".

1. Find the open pull requests and the open tasks of `acme-example/engine`.
2. Read task proof, due times, and `waits_for`. List only facts that block the release.
3. Find the open asks about the release.
4. Search open issues with the label `release-blocker`.
5. Answer in this shape:
   • First line: "*Ready to release*" or "*Not ready: <N> blockers*".
   • One line for each blocker, with its owner and link.
   • The last line names who runs the release. Illo does not release.
