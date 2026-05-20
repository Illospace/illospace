"""Project Context domain services.

The package owns durable project resources, backend-owned connectors,
snapshotting, materialization, upload handling, Vault access, permission scope,
and workspace manifests. Run code should depend on these APIs instead of
knowing connector-specific workspace layouts.
"""
