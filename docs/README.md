# Illo Brain Docs

Start here when you want to understand, run, or extend Illo Brain.

## Public Entry Points

- [Architecture](architecture.md) - repo layout, runtime boundaries, and subsystem map.
- [Configuration](configuration.md) - environment variables, secrets, and private state.
- [Server Setup](server-setup.md) - canonical single-server team deployment runbook.
- [Deployment](deployment.md) - deployment overview and advanced native notes.
- [Cycles](cycles.md) - scheduler-owned recurring jobs.
- [Illo-QA Close Criteria](qa-close-criteria.md) - required post-deploy run
  evidence before an `[Illo-QA]` issue is completed.
- [Project Workspaces](project-workspaces.md) - Project root, thread draft, base, conflict, and publish semantics.
- [Workspace Tool Installer](workspace-tools.md) - opt-in persisted tool bundles for team-specific skill dependencies.
- [Security Model](security-model.md) - secrets, tool execution, browser sessions, and data boundaries.
- [Reconstructive Memory Rewrite](reconstructive-memory-rewrite.md) - no-legacy proposal for replacing passive memory retrieval with active evidence reconstruction.
- [Personal Agent Connections MVP](personal-agent-connections-mvp.md) - implementation plan for connecting Illo with Hermes and OpenClaw.
- [Universal Thread Context Ingress PRD](prd-universal-thread-context-ingress.md) - product plan for personal agents submitting context into Illo and Universal Threads.
- [Dependency Licensing](dependency-licensing.md) - Apache 2.0 project policy and third-party review notes.

Planning notes, runtime journals, memory exports, logs, and operator notes belong
in `.illo/` or another git-ignored private directory.
