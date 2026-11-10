# Connectors

Illo calls only the tools that a workspace connector file lists.
Each person connects Google with their own sign-in.
Team members do not need the host's credentials.
The brain never receives a token, a client secret, or a sign-in code.

## Google for a person

1. Create a Google Cloud project.
2. Enable the Gmail API, Calendar API, and Drive API.
3. Set up the OAuth consent screen. Use Internal for a Google Workspace team.
4. Add these three read-only scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/calendar.readonly`
   - `https://www.googleapis.com/auth/drive.readonly`
5. Create an OAuth client of type Web application for a public Illo address.
6. Add `<ILLO_PUBLIC_URL>/oauth/google/callback` as its redirect URI.
7. Set `ILLO_GOOGLE_CLIENT_ID` on the Illo host.
8. Store the client secret with `illo secret set google-client-secret`.
   You can instead set `ILLO_GOOGLE_CLIENT_SECRET` in the service environment.
9. Set `ILLO_VAULT_KEY` to a Fernet key. Keep it outside every git checkout.
10. Add `connectors/google.yaml` to the workspace:

```yaml
kind: google
scope: personal
scopes:
  - https://www.googleapis.com/auth/gmail.readonly
  - https://www.googleapis.com/auth/calendar.readonly
  - https://www.googleapis.com/auth/drive.readonly
```

Restart Illo or reload the workspace. Send `connect my Google` in a Slack DM.
Open the one-time link. It expires after 10 minutes.
Use HTTPS for a public callback and keep the same browser for the whole sign-in.
Illo binds the callback to a secure, host-only browser cookie.
The Google account must match one of your email addresses in `people.yaml`.
Illo revokes a new grant if the account does not match.
The callback stores your grant with encryption in Postgres.
Only your untainted DM runs and agent runs can use your grant.
Send `disconnect my Google` in a DM to revoke and delete it.
Run `illo connectors status` on the host to see owners and expiry times.
This command shows no token or secret.

For a host without a public address, create a Desktop OAuth client.
Use a loopback `ILLO_PUBLIC_URL`, such as `http://127.0.0.1:8000`.
The DM then gives you the Google sign-in address directly.
The browser can fail at the final local redirect address. This is expected.
Paste that full address as the only text in the same person's DM with Illo.
Illo accepts only its configured callback address. Other messages continue to the assistant.
If a callback address appears inside other text, Illo removes it before the brain sees that text.
Illo takes this reply before a brain run and removes the address from the event ledger.
Illo also drops OAuth access-log records. Keep proxy access logs free of OAuth query strings.

The tools are `mail_search`, `mail_get`, `calendar_list`, `drive_search`, and `drive_get`.
They all have risk `read` and `personal = true`.
`calendar_list` accepts `from` and `to` as dates with time zones.
Mail reads return plain text and attachment names. They do not download attachments.
Drive exports Docs and Slides as plain text. It exports Sheets as CSV.
For other files, it returns the name, type, and link.
Results are marked as outside data.
A later write to a shared destination becomes a draft for the same person.

## Team MCP connectors

Create a file such as `connectors/docs.yaml`:

```yaml
kind: mcp
scope: team
url: https://mcp.example.com/mcp
auth:
  type: bearer
  secret: acme-docs-token
timeout_s: 20
max_bytes: 262144
tools:
  search:
    risk: read
    description: Search the team documents.
    tainted: true
  create_ticket:
    risk: write_internal
  send_message:
    risk: send_outside
```

Store the bearer with `illo secret set acme-docs-token`.
The secret name is safe to keep in the workspace. The secret value is not.
A file can omit `key`; its file name supplies the key.
The old list form for `tools`, with `name` and `risk` fields, still works.

`connectors_list` shows the configured team connectors and tools.
`connectors_call` takes `connector`, `tool`, and `arguments`.
Illo caches the schemas from the remote server.
A tool must exist both in the file and on the remote server.
A remote server cannot expose an extra tool through Illo.
The response has a structured `outside_data` boundary with `trusted: false`.
The call has a time limit and a byte limit, including the wire response.
The sensor is `connector:<key>`.

The file supplies the risk before the tool server runs its three checks.
Every call uses destination `outside:<key>`, including a read with remote arguments.
After a personal read, a connector call needs a draft that the requester approves.
`send_outside` creates a draft. A run without a person cannot create that draft.
An approved `connectors_call` draft uses the registered connector performer.
A tainted run can call only a tool with both `risk: read` and `tainted: true`.
A write with `tainted: true` makes the workspace invalid.

### MCP OAuth

Use this auth block instead of a bearer:

```yaml
auth:
  type: oauth
  client_id: acme-illo-client
  client_secret: acme-docs-client-secret
  scopes: [read]
  redirect_uri: http://127.0.0.1:8765/callback
```

`client_secret` names a vault secret. Omit it for a public OAuth client.
Run `illo connector login docs` on the host.
Open the printed address. Paste the final redirect address at the hidden prompt.
Illo checks the redirect URI and state, and uses PKCE with S256.
The local callback need not have a listener.
Illo stores the token in an encrypted team grant and refreshes it when needed.
A long-lived access token also works.

Illo discovers the protected resource metadata and the authorization server metadata.
If needed, set `authorization_url` and `token_url` in the auth block.
Without a client ID, Illo uses dynamic registration when the server supports it.
For client credentials, set `client_id`, `client_secret`, and `token_url`.
For an existing token, `auth.secret` can name a vault value with JSON fields
`access_token` or `refresh_token`. Set `token_url` for a refresh token.

## Shared drive

Shared reads use `drive_shared_search` and `drive_shared_get`.
These names keep the personal Drive tools' safety contract intact.
A shared read does not set `personal_read`.
Shared reads are available to team runs, person runs, and routines. Tainted runs cannot use them.

Use a service account and grant it read access to the shared drive or folders.
Store its JSON with `illo secret set acme-drive-service-account`.
Keep the original JSON outside every git checkout, with file mode 600.
Create `connectors/drive-shared.yaml`:

```yaml
kind: google
scope: team
drive_id: acme-drive-id
scopes:
  - https://www.googleapis.com/auth/drive.readonly
auth:
  type: service_account
  secret: acme-drive-service-account
```

You can use `folder_ids: [acme-folder-id]` instead of `drive_id`.
Folder mode reads direct children of the listed folders.
It does not include nested folders automatically.
Illo checks each file's drive or parent before it exports content.

You can instead give the team a copy of one person's Google grant.
That person first connects Google in a DM.
Set `team_grant: true` and omit `auth` in the shared connector file.
Then run `illo connector login drive-shared --person ada` on the host.
The copied grant can have wider Google access, but Illo restricts reads to the configured drive or folders.
Google revocation also revokes a copied grant.
A DM disconnect deletes the personal grant. Delete the shared configuration to stop team reads as well.

## Limits

Illo uses the official MCP 1.x client with Streamable HTTP.
It does not expose a shell, a file tool, or a destructive connector risk.
Google retries rate limits and temporary server failures with a short backoff.
Search results include a page token when more results exist.
The first tools return one bounded page. They do not crawl a mailbox or drive.
The optional source hooks register mail, personal Drive, and shared Drive when the search module supports them.
Tests use fake clients and HTTP responses. They do not call a real account.

## Email sender checks

Set `ILLO_EMAIL_AUTHSERV_ID` to the receiving server's identity, such as `mx.google.com`.
Illo uses only the first `Authentication-Results` header with that identity.
If the setting is blank, Illo uses only the topmost `Authentication-Results` header.
The receiving server must delete incoming headers that claim its identity.
It must put its own results above any headers that the sender supplies.
Illo requires exactly one DMARC result: `pass`, with `header.from` equal to the From domain.
SPF and DKIM results alone do not give a sender authority.
A failed check treats the sender as an outsider.
