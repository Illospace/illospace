# Security Policy

Illo Brain handles model provider keys, vault secrets, workspace files, browser
sessions, and potentially sensitive thread/memory data. Please treat security
reports and leaked data with care.

## Reporting vulnerabilities

Please do **not** open a public GitHub issue for vulnerabilities, leaked secrets,
or private data exposure. Use GitHub private vulnerability reporting when
available, or contact the maintainers privately through the security contact
listed in the public repository settings. If no private channel is configured
yet, create a minimal issue that only asks for a secure contact path and does not
include exploit details.

## What to include

- Affected component and version/commit if known.
- Reproduction steps or proof of concept.
- Impact assessment.
- Any relevant logs with secrets and personal data removed.

## Secret hygiene

Never commit `.env`, provider API keys, database dumps, uploaded files, generated
journals, runtime logs, or private operator prompt files. The default `.gitignore`
keeps these under `.illo/` or other ignored runtime paths.
