# Domain Versioning And Conflicts

Query before update. When expected versions are known, pass them so concurrent edits do not disappear. On conflict, reload the record, compare fields, and explain the merge choice. Prefer archive over permanent delete by default.
