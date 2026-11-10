# Email, routines, and public pages

## Email setup

Use a mailbox for Illo. Set these environment variables outside the workspace:

| Variable | Purpose | Default |
|---|---|---|
| `ILLO_EMAIL_HOST` | IMAP server. This enables the email timer. | Disabled |
| `ILLO_EMAIL_PORT` | IMAP TLS port. | `993` |
| `ILLO_EMAIL_USER` | Mailbox account. | None |
| `ILLO_EMAIL_PASSWORD` | Mailbox password. | None |
| `ILLO_EMAIL_PASSWORD_SECRET` | Vault secret name instead of an environment password. | None |
| `ILLO_EMAIL_FOLDER` | Mailbox folder. | `INBOX` |
| `ILLO_EMAIL_POLL_SECONDS` | Time between checks, in seconds. | `60` |
| `ILLO_EMAIL_MAX_BYTES` | Maximum message size, in bytes. | `2097152` |
| `ILLO_EMAIL_AUTHSERV_ID` | Trusted receiving server in `Authentication-Results`. | None |
| `ILLO_SMTP_HOST` | SMTP server. | Disabled |
| `ILLO_SMTP_PORT` | SMTP TLS port. | `465` |
| `ILLO_SMTP_USER` | SMTP account. | IMAP account |
| `ILLO_SMTP_PASSWORD` | SMTP password. | None |
| `ILLO_SMTP_PASSWORD_SECRET` | Vault secret name instead of an environment password. | None |
| `ILLO_SMTP_FROM` | Plain sender address for Illo. | None |
| `ILLO_SMTP_STARTTLS` | Require STARTTLS instead of implicit TLS. | `false` |

For STARTTLS, set the port to the provider's STARTTLS port, usually `587`.
Both protocols verify the server certificate. Neither supports plaintext login.
Vault secrets need `ILLO_VAULT_KEY`. Use `illo secret set <name>` to store each password.
Each team uses its own mailbox account. Do not use another team's credentials.

The receiving server must remove incoming authentication headers that claim its own identity.
It must put its own results above any headers that the sender supplies.
Illo trusts one `Authentication-Results` header:
- With `ILLO_EMAIL_AUTHSERV_ID` set, the first header with that server name.
- With the setting empty, the topmost header.

That header must hold exactly one `dmarc=` result. The result must be `pass`.
Its `header.from` must equal the From domain. Illo does not guess parent domains.
SPF and DKIM results alone give no authority.
Illo reads comments and quoted strings in the header correctly, so a `;` inside them starts no new result.
Missing or failed authentication makes the sender an outsider, with no team authority.

In `illo.yaml`, optional settings are:

```yaml
email:
  unknown_senders: ignore  # Or notify the default owner in Slack.
  reply: both             # Or slack or email.
```

Unknown senders get no model run and no reply.
A forwarded email from a known sender runs as that person, with the `tainted` profile. It gets no personal connector.
The optional notice contains the subject. It does not grant authority to the message.
Known senders get replies only at their authenticated From address and their Slack DM.
Illo ignores `Reply-To`. Email replies keep `In-Reply-To` and `References`.
The gateway stores each Message-ID once before IMAP marks the message as seen.
Attachments expose only their names and sizes. Illo does not read their contents.

`email_draft` always creates a draft, including mail to team members.
This follows the architecture's requirement that outbound email needs approval.
A direct reply to a person's own request follows the reply setting above.
The draft performer supports both `email` and `email_draft` kinds.
The asks area owns draft cards and the approval action.

## Routines

Put each routine in `routines/<name>.md` in the workspace:

```markdown
---
schedule: '0 9 * * 1-5'
principal: person:ada
destination: dm:ada
enabled: true
skills: []
---
Read my open asks. Report what needs my decision.
```

The file name supplies the name unless front matter sets `name`.
Schedules use five cron fields and the team's IANA time zone.
Destinations accept `dm:<person key>`, `channel:<configured name>`, and `none`.
Existing `person:<key>` and `channel:<configured id>` destinations also work.
The optional skills list names workspace skills. Illo adds their text to the prompt.
Workspace reloads apply file changes. `illo routines list` shows the stored times and status.

A team routine gets the `routine` profile and no personal connector.
A person routine gets that person's `person_dm` profile.
A reply with personal data needs a draft before it goes to another destination.
The `routine_delivery` performer sends that reply after approval.

Illo claims a scheduled time before it starts the gateway run.
A restart cannot run that time twice. A missed time less than six hours old runs once.
An older time is skipped. Illo then schedules the next future time.
A crash after a claim can lose that run. Illo prefers this to a duplicate action.

## Public pages

`web_read_public_page` accepts one URL from the inbound event, or that host's root page.
In a tainted run, it accepts only the first URL of the event, and only once.
It rejects a call after the run reads personal or private data (`personal_read`).
The tool checks every DNS answer and each redirect. It connects to a checked IP address.
It refuses private, loopback, and translated IPv6 addresses (`64:ff9b::/96`, `64:ff9b:1::/48`, `::/96`).
It keeps the original host for TLS verification and the HTTP Host header.
It never uses environment proxy settings.

HTTPS is the default. `ILLO_WEB_ALLOW_HTTP=true` also permits HTTP.
The tool permits three redirects, ten seconds, and two megabytes.
It accepts only HTML and plain text. It returns at most 15,000 text characters.
It does not run JavaScript. The returned text is marked as outside data.
