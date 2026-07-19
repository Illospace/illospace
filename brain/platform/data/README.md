# Runtime Data

Runtime state must not be written into this source directory. Diagnostics,
scheduler pipelines, and learning tools store private artifacts under
`ILLO_PRIVATE_HOME` (the production deployment mounts it at `/data/private`).

This README remains as a guardrail for older installations that may still have
ignored files here; new writers must use `brain.kernel.config.PRIVATE_HOME`.
