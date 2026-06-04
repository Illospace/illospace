# Workspace Tool Installer

Workspace tools are opt-in, persisted tool bundles that a team can install when
an Illo skill needs external executables. They avoid baking niche tools into the
base worker image for every team.

## Contract

- Bundles are cataloged in `deploy/compose/workspace-tools.json`.
- Illo manages them through `manage_workspace_tools`.
- Installs are workspace-scoped by `org_id`.
- Installed files live under `ILLO_WORKSPACE_TOOLS_ROOT`, defaulting to
  `/data/private/workspace-tools`.
- The updater sidecar watches `ILLO_WORKSPACE_TOOLS_REQUEST_FILE` and executes
  curated installer profiles through `deploy/scripts/workspace-tools.sh`.
- Successful installs write `illo-tool.json` with status, health, bin paths, and
  metadata.
- Agent command environments prepend installed bundle `bin` paths for the active
  workspace.

## First Bundle

`aws-diagrams` installs:

- PlantUML `1.2026.4`
- Java and Graphviz through a persisted micromamba environment
- awslabs AWS icons for PlantUML `v19.0`

This supports skills such as `aws-architecture-diagrams` without making Java,
Graphviz, or PlantUML mandatory for every Illospace team.

## Agent Flow

1. `manage_workspace_tools(action="catalog")`
2. `manage_workspace_tools(action="status", bundle_id="aws-diagrams")`
3. With user approval, `manage_workspace_tools(action="install", bundle_id="aws-diagrams")`
4. Later runs get the installed bundle on `PATH`.
5. Skills can call `manage_workspace_tools(action="check", bundle_id="aws-diagrams")`
   before claiming rendered artifacts are available.

## Security Boundary

The model cannot submit arbitrary installer scripts. It can only request a
cataloged bundle id. The host controller runs curated installer profiles, writes
status/log files, and stores tools in the shared private volume.
