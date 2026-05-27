# Security Model

Illo Brain handles model credentials, workspace files, browser sessions, and
potentially sensitive memory/thread data. Self-hosters should treat it as a
trusted local automation system, not as a hardened multi-tenant sandbox.

## Secrets

- `.env`, `production.env`, database dumps, logs, uploads, and operator notes
  must stay out of git.
- Provider credentials should live in Illospace's encrypted DB-backed
  credential storage; never paste them into repo files, logs, issues, or PRs.
- Runtime-private local state should live under `.illo/` or another
  `ILLO_PRIVATE_HOME`.
- Database-backed vault entries are encrypted and scoped by user/org metadata,
  but agents may still be granted access through configured tool policies.
- Agent command tools can mount Vault entries through run-scoped `secret_env`
  bindings. The trusted runtime resolves the Vault key, injects the value only
  for that tool call, and redacts the mounted value from command output and
  artifacts; prompts and public traces should contain only Vault key names and
  environment variable names.
- Never paste real secrets into issues, PRs, logs, screenshots, or fixtures.

## Tool Execution

Agent runs can read files, write files, execute shell commands, browse the web,
and call provider APIs depending on the configured tool surface. Run Illo Brain
only against repositories and hosts where that level of automation is acceptable.

Recommended defaults:

- keep local Codex/auth fallbacks disabled outside single-user laptop dev;
- use project context scopes to keep runs focused;
- keep destructive external actions behind explicit human workflows;
- review generated artifacts before deploying or publishing them.

## Browser Runtime

Browser sessions run server-side and can interact with websites using local
network access. Configure host firewalls and network policy accordingly. Avoid
using privileged browser profiles or persistent cookies unless the deployment
explicitly needs them.

## Reporting

Report vulnerabilities privately as described in [SECURITY.md](../SECURITY.md).
Do not open public issues with exploit details, secrets, transcripts, or private
data.
