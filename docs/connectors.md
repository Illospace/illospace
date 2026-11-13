# Connectors

Illo calls only the tools that a workspace connector file lists.
Each person connects Google with their own sign-in.
Team members do not need the host's credentials.
The brain never receives a connector token, a client secret, or a sign-in code.
The model sign-in below has a separate, per-run credential path.

## Connect in chat

Talk to Illo in your own Slack DM. Ask "What tools are connected?" to see the state.
Illo shows your own sign-in state and the team's connections. It shows no other person's account details.

For Google:

1. The owner writes `connect Google` or `set up Google`.
2. Illo gives the owner one numbered message with the Google console links.
   Google requires one person to create the client one time for the whole team.
3. The owner taps `Enter the Google client` and pastes the Client ID and Client secret in the form.
   Illo checks the pair with Google before it replaces the saved pair.
   A failed check keeps the old pair. No restart is needed.
4. Each person writes `connect my Google` and opens the sign-in link.

`connect my calendar`, `connect my mail`, `connect my Gmail`, `connect my Drive`, and
`connect my Google account` start the same sign-in. Letter case and final punctuation do not matter.
A person who is not an owner gets the owner's name when Google needs team setup.
Write `disconnect my Google` to revoke and delete your grant.
Write `connect Google` to see the team state or replace the client through a new form.
If the callback address changes, Illo shows the new address to add in the Google console.
The Google Internal audience needs a Google Workspace account.

For a team MCP server:

1. The owner writes `connect the MCP server at https://mcp.example.com/mcp as acme-crm`.
2. Illo checks the server's sign-in method before it shows the `Add` / `Do not add` card.
   The card shows the server address. For OAuth, it also shows the sign-in host.
   The sign-in host can differ from the server host.
3. If the server needs a key, `Add` opens a private form for it.
   For OAuth, Illo registers a public client when the server permits it.
   Otherwise, the owner enters a Client ID and an optional Client secret in a private form.
4. Illo sends a `Sign in` button in the owner's DM. Open it within 10 minutes.
   Finish sign-in in that browser. The page then says to go back to Slack.
5. Illo reads the tool list. A failed sign-in or read saves no connector, grant, or key.
6. Illo saves `connectors/acme-crm.yaml` with no allowed tools and shows up to 30 tools in the DM.
7. The owner names the tools to allow and labels each as read or write.
   Illo shows an `Allow` / `Do not allow` card before it saves these permissions.
   Each write call needs its own approval card.

Only an active owner can add, allow, remove, or sign in to a team connector.
Write `connect acme-crm again` or `sign in to acme-crm` to replace its sign-in.
If a sign-in ends, Illo sends one DM with a `Sign in` button to the owner who signed in.
The button sends a new link. Calls to that connector are refused until the new sign-in.
These commands keep the allowed tools. They need no new Add or Allow card.
If a sign-in ends, calls stop. The sensor `connector:acme-crm` shows the fault.
Illo sends one DM with a new sign-in link to the owner who signed in.
Personal MCP connectors are not available. Google keeps its personal sign-in flow.
Removal needs a card. It removes the connector file and its saved key.
The server's tool descriptions are outside data. They cannot grant a tool or change its risk.
The file keeps all discovered names in `discovered_tools`. Only `tools` grants access.
Connector changes make a local Git commit when the workspace is a Git repository.
`workspace.git_push: true` also pushes that commit. A failed push leaves the file and local commit saved.

Never paste a key or password in a chat message. Use only the form that Illo opens.
Illo encrypts the values in its vault. It keeps them out of events, cards, audit values, and workspace files.
The app needs `ILLO_VAULT_KEY`, as the installer sets it.
No new environment variable is needed for these chat flows.
See [the public sign-in address](install.md#the-public-sign-in-address) for a browser address separate from the internal service address.

## AWS

The owner writes `connect AWS` in their own Slack DM.
Illo shows `Enter the AWS key`. The button opens a private form.
Enter the Access key ID, Secret access key, and Region. The default region is `us-east-1`.
Use an IAM user key with only the reads that the team needs.
Illo checks `sts:GetCallerIdentity` before it saves the key. A failed check saves nothing.
This check confirms the key's identity. It does not prove that its IAM policy allows only reads.
Illo stores the key in the encrypted vault and makes a local commit for `connectors/aws.yaml`.
A person who is not an owner gets the owner's name.
Write `disconnect AWS` and approve the removal card to delete the file and the saved key.

The saved configuration has this form:

```yaml
kind: aws
scope: team
region: us-east-1
customer_data: true
auth:
  type: access_key
  secret: aws-illo
services: [logs, cloudwatch, autoscaling, elbv2, codebuild, ecs, lambda, rds]
```

The vault value is JSON with `access_key_id` and `secret_access_key`.
No host AWS profile, environment credential, shared credential file, or instance credential supplies a key.
No new environment setting is needed. `ILLO_VAULT_KEY` keeps its existing role.
The connector accepts only the eight services above. The owner can choose a smaller list.
Services that expose secrets or bulk customer files are refused, including S3, SSM, IAM, and Secrets Manager.

`aws_read` takes `service`, `operation`, `parameters`, and an optional `region`.
Use API names such as `DescribeLogGroups`, `FilterLogEvents`, `GetMetricData`, or `DescribeTargetHealth`.
For Logs Insights, call `StartQuery`, then `GetQueryResults` with the query ID.
Illo also allows `StopQuery` on logs. Other operation names must start with
`Describe`, `Get`, `List`, `Filter`, `BatchGet`, or `Lookup`.
Illo removes Lambda code links and environment values, RDS passwords, CodeBuild environment values,
and ECS environment and secret values. Lambda layer code downloads are refused.
Unknown operations get a fixed refusal. The IAM policy of the key is the main access limit.

Each call reads one page. Keep the returned page token to request the next page.
The result has outside-data markers. A page over 262144 bytes has `truncated: true` and keeps its page token.
Calls use a worker thread, 5-second connect and 20-second read timeouts, and at most two retries.
AWS error text never reaches the model. Fixed messages can include a safe AWS error code.
The sensor is `connector:aws`. `connectors_list` shows the region and services, never the key ID.
Person DMs, team channels, routines, and deep runs can read AWS. Tainted runs, outsiders, and agent requests cannot.
An AWS call closes the web for the rest of the run, even with `customer_data: false`.
Read the web before AWS when a question needs both sources.

## The model sign-in

Each member can use their own ChatGPT subscription with `brain.auth: chatgpt` and `brain.runtime: openai_responses`.
This is the default `brain.person_credential: own` mode.
The workspace owner can choose `brain.person_credential: team` to use the sign-in of `brain.team_credential` for untainted person runs.
That key must name an active person. Only that person needs to connect ChatGPT.
This changes only who pays for the model. Personal tools keep the requester's grants and data.
The requester's messages then pass through the selected person's ChatGPT account.
Set `ILLO_VAULT_KEY` in the main app. No connector YAML file or host sign-in file is needed.

ChatGPT sign-in needs no public Illo address or inbound HTTP request.
`ILLO_PUBLIC_URL` can stay at the internal address that the brain uses.

1. Send `connect my ChatGPT` in your own Slack DM with Illo.
2. Open `https://auth.openai.com/codex/device` and sign in to your own ChatGPT account.
3. Enter the one-time code from the DM. It expires 10 minutes after your request.
4. Illo sends you a DM after sign-in.

If ChatGPT refuses the code, turn on device-code sign-in in the ChatGPT security settings.
You can also send `connect my ChatGPT with a link` in the DM.
Illo then sends the OpenAI sign-in link directly. This command cancels your previous sign-in attempt.
Illo also uses this link flow if it cannot get a device code.
Open the link within 10 minutes. The final local callback address can fail to load. This is expected.
Paste that full address into the same DM and thread. Send only that address.

Illo sends `http://127.0.0.1:1455/auth/callback`, as the checked Codex source does.
A pasted callback may use `127.0.0.1` or `localhost`. Illo accepts Slack link wrappers and escaped ampersands.

Illo replies: `ChatGPT is connected as <email>. I use your subscription for your requests.`
The device flow sends this reply without a paste.
If a started device sign-in expires, Illo sends one failure notice.
A temporary provider error keeps the sign-in pending and increases the poll interval.
The account email must match an address of the person in `people.yaml`, ignoring letter case.
Illo reads the ID token from the fixed HTTPS token endpoint only for this comparison.
It refuses missing email claims and revokes rejected tokens at the published revocation endpoint.
For a shared account, explicitly list its email in `brain.chatgpt_allowed_accounts` in `illo.yaml`.
This team list defaults to empty.
Enable device sign-in in your ChatGPT security settings, or ask your workspace administrator to enable it.
Only enter a device code if you asked Illo to connect your own account.
Send `ChatGPT status` to check the grant. Send `disconnect my ChatGPT` to delete it from Illo.
In `team` mode, `ChatGPT status` names the shared sign-in and shows whether it is connected.
Both connect commands tell other members that they do not need to connect. They start no sign-in for those members.
The team person can connect as before. Their disconnect reply warns that all members lose the model sign-in.
Another member can disconnect only their own unused grant. The team grant stays connected.
Only these exact DM commands can change the sign-in. The model can only check its status.
Disconnect attempts OpenAI token revocation before it deletes the grant and cancels pending sign-in attempts.
A provider error does not block local deletion.
If revocation fails, an old database backup plus the vault key can still hold a usable token.
Use the ChatGPT account security settings to revoke it in that case.
Disconnect does not sign you out of other OpenAI clients.
A crash during a token refresh can need one new sign-in.
The provider can rotate the token before Illo commits it to the database.
Illo cannot recover that new token after a crash.
The operator can run `illo chatgpt status` to list connection states and expiry times without secrets.
Its first line shows the person credential mode and the team person key.
See [Workspace files](workspace.md) for team credentials and the optional API-key fallback.
Check that the provider's terms allow this use. An API key is the alternative.

## Google for a person

The chat steps above replace the manual client and file setup.
As an alternative, set `ILLO_GOOGLE_CLIENT_ID` and `ILLO_GOOGLE_CLIENT_SECRET` in the service environment.
Each environment value takes priority over its vault value.
The vault names are `google-client-id` and `google-client-secret`.
An environment change needs a service restart. A vault change does not.
A manual personal connector file is still valid:

```yaml
kind: google
scope: personal
scopes:
  - https://www.googleapis.com/auth/gmail.readonly
  - https://www.googleapis.com/auth/calendar.readonly
  - https://www.googleapis.com/auth/drive.readonly
```

Send `connect my Google` in a Slack DM.
Open the one-time link. It expires after 10 minutes.
Use HTTPS for a public callback and keep the same browser for the whole sign-in.
Illo binds the callback to a secure, host-only browser cookie.
The Google account must match one of your email addresses in `people.yaml`.
Illo revokes a new grant if the account does not match.
The callback stores your grant with encryption in Postgres.
Only your own untainted DM runs and your person routines can use your grant.
An agent request never uses it.
Send `disconnect my Google` in a DM to revoke and delete it.
Run `illo connectors status` on the host to see owners and expiry times.
This command shows no token or secret.

For an existing host without a public address, the manual Desktop client setup remains available.
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
After such a read, a connector call is refused, and the web tool refuses a page.

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

`customer_data` defaults to `true` for MCP and AWS connectors.
Set `customer_data: false` only for a server with public or team-only content.
The field does not apply to Google connectors, which keep their personal-read rules.
A customer-data connector call closes public page reads and provider web search for the rest of that run.
After that call, another customer-data connector stays available.
A non-customer connector needs a draft that the requester approves. Without a person, Illo refuses the call.
An internal deep run inherits the flag when its parent read customer data.
Deep scouts, requests, and briefs can call team MCP read tools. They cannot call connector writes or personal Google tools.

The file supplies the risk before the tool server runs its three checks.
Every call uses destination `outside:<key>`, including a read with remote arguments.
After a personal read, a connector call is refused.
The tool handler also refuses a direct call after a personal read.
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
The chat flow above uses `/oauth/mcp/start` and `/oauth/mcp/callback` at the public sign-in address.
It uses authorization code with PKCE S256 and the MCP resource identifier.
The protected resource metadata supplies the scopes. `auth.scopes` can narrow them.
Dynamic registration uses the name `Illo` and the public callback address.
The private client form supplies the client when the server has no registration endpoint.
For clients with a secret, Illo supports HTTP Basic and form authentication.
Client secrets and tokens stay encrypted. The connector file holds only the client ID and secret name.
The browser state belongs to one person and one connector. It expires after 10 minutes and works once.
The callback also needs the secure browser cookie from the start route.

For a host without a public HTTPS address, Illo shows `illo connector login <key>`.
Configure the connector file first for this host flow. The command remains available for an existing connector.
Run `illo connector login docs` on the host.
Open the printed address. Paste the final redirect address at the hidden prompt.
Illo checks the redirect URI and state, and uses PKCE with S256.
The local callback need not have a listener.
Illo stores the token in an encrypted team grant and refreshes it when needed.
A long-lived access token also works.

The CLI and chat share the same discovery code.
Illo first checks the MCP server's 401 challenge for a protected resource metadata address.
It also tries the protected resource well-known paths, with and without the MCP path.
For an issuer with a path, it tries path insertion, the appended path, then OpenID discovery.
The issuer must match its metadata exactly.
Each outside request uses a checked public HTTPS address, a time limit, and a byte limit.
Illo refuses redirects and private DNS answers. It pins the checked IP for the request.
An MCP connector therefore needs a public HTTPS address. A server on a private network is refused.
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
With `connectors/google.yaml`, `search_all` also searches the person's mail and Drive. With a team Google file, it also searches the shared drive.
A team MCP call has a time limit (`timeout_s`, default 20, maximum 120 seconds) and a byte limit (`max_bytes`, default 262144, maximum 1048576).
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

## Task connectors

A connector is the only place that knows one outside tool.
The task core calls registered functions. It never imports that tool.
A team can use tasks with only the generic wake types.

A connector supplies four parts:

- **Signals:** outside changes that create or move a task.
- **Context:** facts that a person or agent can read for a task.
- **Actions:** writes to the outside tool, with explicit approval rules.
- **Checks:** facts that a Done rule can use.

### GitHub example

GitHub registers the `github_issue` wake type and the `ticket_closed` and `change_merged` checks.
Its poller follows ticket links on open tasks. Comments supply activity and progress.
A needs-input label asks the reviewer. A completed issue returns the work for its Done checks.
The context tools read issues and changes through the shared client.
Task wake actions create, assign, comment, reopen, or restart the ticket without a card.
Each write has an audit row with the task as its target. Other writes keep their approval rules.
Outside comments remain data. They never grant authority.

### Write your own connector

1. Define a Pydantic wake config. Keep the outside fields in your connector module.
2. Call `illo.tasks.connectors.register_wake(name, model, function)`.
   The async function accepts `(ctx, conn, task, agent, reason, event)`.
   It returns `WakeResult(ok, text, links=[])`.
3. Call `illo.tasks.checks.register(name, async_check)` for each proof check.
4. Call `illo.tasks.connectors.register_signal(name, handler)` for source signals.
   Dispatch them with `signal(name, ctx, conn, **facts)`.
5. Call `register_links(kind, handler)` to watch links on open tasks.
   Dispatch fresh facts with `changed(ctx, conn, kind, **facts)`.
6. Call `register_lane(handler)` if collector activity can prove task liveness.
7. Register context tools with the existing tool registry.

Register installed connectors before workspace validation.
Wake retries keep the same event ID. Use it to prevent duplicate writes.
A wake payload tells the holder where to look. It does not carry the work.
