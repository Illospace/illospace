# Workspace Tool Installer

Workspace tools are opt-in, persisted tool bundles that a team can install when
an Illo skill needs external executables. They avoid baking niche tools into the
base worker image for every team.

## Contract

- Bundles are cataloged in `deploy/compose/workspace-tools.json`.
- Illo manages them through `manage_workspace_tools`.
- Installs are workspace-scoped by `org_id`.
- Bundle catalog entries may declare `runtime.auth_profiles` for tools that
  need credentials at execution time.
- Per-user workspace tool config is keyed by `org_id + user_id + bundle_id`
  and stores non-secret preferences plus credential references only.
- Installed files live under `ILLO_WORKSPACE_TOOLS_ROOT`, defaulting to
  `/data/private/workspace-tools`.
- The updater sidecar watches `ILLO_WORKSPACE_TOOLS_REQUEST_FILE` and executes
  curated installer profiles through `deploy/scripts/workspace-tools.sh`.
- The updater sidecar is exposed as the `host_controller` runtime service; if a
  workspace tool status says it is waiting for the host controller, restart
  `host_controller` through `manage_runtime_services`.
- Successful installs write `illo-tool.json` with status, health, bin paths, and
  metadata.
- Agent command environments prepend installed bundle `bin` paths for the active
  workspace.
- Agent subprocess tools can materialize declared runtime auth profiles for a
  single invocation through `workspace_tool_auth`; `exec_command` also
  auto-detects installed workspace tool profiles from command names.

## Runtime Auth Profiles

Workspace tool installs are shared team state; credentials are not. A bundle
that needs authentication declares a runtime auth profile instead of storing
secrets on the installation record.

Runtime profiles describe:

- which commands they apply to, for example `codex`;
- where credentials come from, such as an originating user's provider
  connection, a workspace API key, or a Vault secret reference;
- how to project that credential into the tool, such as an env var or a
  temporary config file;
- whether the credential is user-scoped or workspace-scoped.

At execution time, trusted runtime code resolves the active `actor_user_id`,
materializes the credential into a temporary environment or file, runs the
tool, redacts secret values from output/artifacts, and deletes temporary files.
The model sees only references and status, never raw secret values.

## Bundles

`aws-diagrams` installs:

- PlantUML `1.2026.4`
- Java and Graphviz through a persisted micromamba environment
- awslabs AWS icons for PlantUML `v19.0`

This supports skills such as `aws-architecture-diagrams` without making Java,
Graphviz, or PlantUML mandatory for every Illospace team.

`codex-cli` installs:

- OpenAI Codex CLI through the generic `npm_package_cli` installer profile
  and npm package `@openai/codex`
- a `codex` wrapper in the workspace tool `bin` path
- a runtime auth profile that resolves the originating user's OpenAI
  Codex/ChatGPT connection and writes a temporary `CODEX_HOME/auth.json` for
  that one subprocess call

## Agent Flow

1. `manage_workspace_tools(action="catalog")`
2. `manage_workspace_tools(action="status", bundle_id="aws-diagrams")`
3. With user approval, `manage_workspace_tools(action="install", bundle_id="aws-diagrams")`
4. Later runs get the installed bundle on `PATH`.
5. Skills can call `manage_workspace_tools(action="get_config", bundle_id="...")`
   or `set_config` to read/write non-secret user preferences and credential
   references for shared tools.
6. Skills can pass `workspace_tool_auth=["codex-cli"]` to subprocess tools when
   a runtime auth profile should be materialized explicitly.
7. Skills can call `manage_workspace_tools(action="check", bundle_id="aws-diagrams")`
   before claiming rendered artifacts are available.

## Security Boundary

The model cannot submit arbitrary installer scripts. It can only request a
cataloged bundle id. The host controller runs curated installer profiles, writes
status/log files, and stores tools in the shared private volume.

Runtime auth materialization follows the same boundary. The model can request a
bundle/profile reference, but only trusted runtime code may resolve provider
connections or Vault secrets and write temporary env/files for a subprocess.
