# Dependency Licensing

Illo Brain is released under the Apache License, Version 2.0. The full license is
in [LICENSE](../LICENSE), and project notices are in [NOTICE.md](../NOTICE.md).

## Source Policy

- New source files should include `SPDX-License-Identifier: Apache-2.0` when the
  file format has a conventional comment syntax.
- Existing files are covered by the repository-level Apache 2.0 license. Bulk
  source-header normalization can happen incrementally.
- Do not add third-party source, generated assets, images, model weights, or
  datasets unless their license is compatible with Apache 2.0 distribution and
  their attribution requirements are documented.

## Third-Party Review

Before tagging a public release:

```bash
python3 -m pip install pip-licenses
pip-licenses --from=mixed --format=markdown

cd frontend
npm install
npm ls --all --json
```

Review both Python and Node dependency trees for copyleft obligations,
noncommercial terms, missing license metadata, and notice requirements. Preserve
the generated reports as release artifacts rather than committing machine-local
dependency output by default.

## Git Dependencies

Runtime dependencies should not point at floating branch names. Pin Git
dependencies to immutable commits or publish them as versioned packages before a
public release.
