# Install Illo

## Push the workspace from the host

Memory changes make local commits in the workspace repository.
The container holds no git credential. The host pushes the workspace.
Use `git -C <workspace> push` in a host timer with the host's own git sign-in.
`workspace.git_push` stays off by default. See [Team memory](memory.md).

Use one Linux host with Git, Bash, Python 3.9 or later, and Docker Engine.
Give the Docker user access to Docker before you start.
Allow at least 3 GB of memory and outbound HTTPS.
No public address is required for Slack Socket Mode.
Docker allows outbound traffic on the default network.
Postgres joins only the internal `db` network with the Illo app.
The brain joins `brainnet` with Illo. It never joins `db`.
Illo and the brain also join the default network for outbound traffic.
For optional hardening, put the brain behind an egress proxy with a model provider allowlist.
Remove its default network access when you set up that proxy.
This install does not configure an egress proxy or restrict Internet destinations.

## Install

1. Clone the repository into a dedicated folder. Use `next` until the rebuild merges into `main`.

   ```sh
   git clone --branch next https://github.com/Illospace/illospace.git
   cd illospace
   ILLO_DEPLOY_BRANCH=next deploy/install.sh
   ```

2. If Compose is missing, the script offers to install it for the current user.
   It downloads the official Linux binary and checks its SHA-256 checksum.
   The plugin path is `~/.docker/cli-plugins/docker-compose`.
   If you use `DOCKER_CONFIG`, the script uses that folder instead.
   A non-interactive install needs this command first:

   ```sh
   deploy/install-compose.sh
   ```

3. The script asks for the Slack bot token and app token with hidden input.
   Press Enter to add them later.
   The script creates two environment files with mode `600`:
   `~/.config/illo/env` for the app and `~/.config/illo/brain.env` for the brain.
   It generates the database password, the vault key, and the brain secret in the app file.
   The brain file holds the provider keys. Only the brain container loads it.
   Set `ILLO_ENV_FILE` to use another absolute path outside every checkout.
   The app file names the brain file in `ILLO_BRAIN_ENV_FILE`.
   A second install keeps both environment files and the workspace.
   It refuses an app file with an `ILLO_PROVIDER_`, `OPENAI_`, or `ANTHROPIC_` variable, in each
   form that Compose reads (`KEY=v`, `KEY = v`, `KEY: v`, `export KEY=v`, a bare `KEY`), and a
   value that names such a variable. It refuses a brain file inside the checkout or the workspace.

4. The script copies `workspace.example/` to `~/.local/share/illo/workspace`.
   Set `ILLO_WORKSPACE_DIR` before the first install to use another folder.
   The app mounts this folder read-write at `/workspace`.
   The app runs as the user who installed it, so the notes writer can write files.
   The brain has no access to this folder or to any host file.

5. The script builds both images, starts Postgres and the services, then checks the app.
   The health check must report the image's revision.
   `illo doctor` prints these lines with a valid example workspace and no Slack token:

   ```text
   The workspace is valid.
   The brain has a provider for each route.
   The database is ready.
   The vault key is valid.
   Slack is optional and has no bot token.
   ```

   When a collector reports in the last day, `illo doctor` also checks its code version.
   See [collector updates](collectors.md#updates).

The environment file uses literal `KEY=value` lines.
Use single quotes around values with spaces, `#`, or `$`.
Do not use shell commands, variable expansion, or multiline values in this file.
Keep secrets outside the checkout and outside the workspace.
The Compose file accepts `ILLO_ENV_FILE`; the scripts always set its absolute path.
Direct Compose commands must pass the same file with `--env-file`.
Compose reads `ILLO_BRAIN_ENV_FILE` from that file. Never put a provider key in the app file.

## Set up the team

DM context uses the Slack `im:history` scope in `deploy/slack/manifest.yaml`.
Illo reads up to 20 earlier messages, with a 6,000-character history limit.
If Slack refuses the read, Illo answers without history. Check the `slack:dm-history` sensor.

1. Create a Slack app from `deploy/slack/manifest.yaml`.
   Enable Socket Mode and install the app in your Slack workspace.
   Add its bot token and app token to the environment file.
2. Edit `people.yaml` in the workspace folder.
   Replace the example people with your team members and their real Slack IDs.
   Edit `illo.yaml` for the team's repositories and model.
   Add Illo to a Slack channel, then mention it. No channel entry is required.
3. Choose the providers and the routes.
   The brain environment file holds the provider keys. See `deploy/brain.env.example`.
   `ILLO_PROVIDER_OPENAI_API_KEY` enables the `openai` provider (Responses, with `store: false`).
   `ILLO_PROVIDER_ANTHROPIC_API_KEY` enables the `anthropic` provider (the Claude Agent SDK).
   `ILLO_PROVIDER_GEMINI_API_KEY` enables the `gemini` provider (Chat Completions, with images).
   See [Set up Gemini](#set-up-gemini).
   `ILLO_PROVIDER_<NAME>_BASE_URL` changes the address of a provider.
   A custom provider also sets `ILLO_PROVIDER_<NAME>_WIRE` (`responses` or `chat`) and
   `ILLO_PROVIDER_<NAME>_CAPABILITIES`, for example `images,web_search,transcription,effort:low`.
   The `chat` wire supports `images` and refuses `web_search`.
   A provider with no key is not available. The `chatgpt` provider needs no key.
   `brain.routes` in `illo.yaml` selects the provider, the model, the effort, and the limits of each run kind.
   See [Workspace files](workspace.md#routes).
   Each provider uses only the Illo MCP tools.
   To use each person's ChatGPT subscription, give a route the `chatgpt` provider and set `brain.chatgpt`.
   Keep `ILLO_VAULT_KEY` in the main app's environment, outside the checkout.
   Each member sends `connect my ChatGPT` in their own Slack DM with Illo.
   The DM gives a device code. ChatGPT sign-in needs no public Illo address.
   Send `connect my ChatGPT with a link` to use the OpenAI link and paste the final local address.
   Set `brain.chatgpt.team_credential` to an active person key.
   `brain.chatgpt.fallback` defaults to `none`. Set it to an API-key provider, for example `openai`, only to allow that key as a fallback.
   The brain needs no sign-in file, refresh token, or host mount.
   Check that the provider's terms allow this use. An API key is the alternative.
   See [The model sign-in](connectors.md#the-model-sign-in) for the device and paste steps.
   See [the brain guide](../brain/README.md) for API details and live measurements.
   The `fake` provider (`ILLO_PROVIDER_FAKE=1`) needs no model key and is only for tests.
4. Recreate the services after an environment change: run `deploy/install.sh` again.
   The brain reads its providers once at start. After a key change, check `GET /providers`
   (run `illo doctor` in the app container).
   You can also use Compose directly, with the saved revision so Compose starts the correct image tags:

   ```sh
   export ILLO_ENV_FILE="$HOME/.config/illo/env"
   export ILLO_REVISION="$(cat "$HOME/.local/state/illo/last-good")"
   docker compose --env-file "$ILLO_ENV_FILE" -f deploy/compose.yaml up -d
   ```

5. Create a collector token for a person from `people.yaml`.
   Replace `ada` with that person's key.
   The token appears once. Store it in the agent's secret store.
   Do not paste it in a ticket or a log.

   ```sh
   docker compose --env-file "$ILLO_ENV_FILE" -f deploy/compose.yaml \
     exec illo illo token create --person ada --label laptop --scope collector
   ```

   A `collector` token reports lanes, asks, and signals. Use `--scope agent`
   for a session client that also sends requests with `illo_submit`.
   [agents.md](agents.md) lists the tools of each scope.

The MCP endpoint is `http://127.0.0.1:8484/mcp`.
It also accepts requests at `/api/mcp`.
Use `Authorization: Bearer <token>`.
A remote collector needs a private tunnel or a reverse proxy with HTTPS.
Publish only the routes that you need, such as `/mcp`, `/oauth/`, and `/list`.
Do not publish Postgres, the brain, `/brain/mcp`, or the admin reload route.

Optional GitHub and mailbox settings appear in `deploy/env.example`.
For email, set `ILLO_EMAIL_AUTHSERV_ID` to the name of the receiving server, such as `mx.google.com`.
[connectors.md](connectors.md) explains the sender check.
For a GitHub App, mount its private key from an external file into the app container.
Set `ILLO_GITHUB_APP_PRIVATE_KEY_FILE` to its container path.
The owner writes `connect Google` in a Slack DM to set up Google through a private form.
The environment variables `ILLO_GOOGLE_CLIENT_ID` and `ILLO_GOOGLE_CLIENT_SECRET` remain an alternative.
Do not put connector secrets in the workspace.

### Set up Gemini

1. Open **API Keys** in Google AI Studio. Select the Google Cloud project and click **Create API key**.
   Enable billing on the project if the model needs a paid tier. [Google AI Studio](https://aistudio.google.com/api-keys)
2. Open **Spend** in Google AI Studio. Select the project, then select **Monthly spend cap** and **Edit spend cap**.
   Set a monthly limit for that project. Google says billing data can lag by about 10 minutes.
   [Spend page](https://aistudio.google.com/spend) · [Google spend cap guide](https://ai.google.dev/gemini-api/docs/billing#project-spend-caps)
3. Put the key in `ILLO_PROVIDER_GEMINI_API_KEY` in the brain environment file.
   Recreate the brain service. In the app environment, run the route smoke check before you change `illo.yaml`.

   ```sh
   illo brain smoke --route person_dm --provider gemini --model gemini-4-neon
   ```

   See [Check a brain route](workspace.md#check-a-brain-route) for the output and payer rules.

## The public sign-in address

The internal `ILLO_PUBLIC_URL` can stay at the address that the brain uses.
To give a browser a separate address, set this block in the workspace's `illo.yaml`:

```yaml
connect:
  public_url: https://illo.example.com
```

Use HTTPS. Do not include credentials, a query, or a fragment.
Expose `/oauth/` and `/list` when those features are used.
For task pages, also expose `/t/` and `/web/`. Prefer the team’s private network.
Keep the brain, `/brain/mcp`, Postgres, and the admin routes private.
A reverse proxy can forward these paths to the local Illo service.
For example, use these Nginx locations inside your HTTPS server block:

```nginx
location /oauth/ {
    proxy_pass http://127.0.0.1:8484;
    access_log off;
}
location = /list {
    proxy_pass http://127.0.0.1:8484;
    access_log off;
}
location = /web/signin {
    proxy_pass http://127.0.0.1:8484;
    access_log off;
}
```

Keep the sign-in query, the list key, and the key of a page sign-in link out of all proxy logs.
Google and MCP OAuth sign-in then complete in the same browser after one click on the DM link.
The MCP callback address is `https://illo.example.com/oauth/mcp/callback`.
The task list uses the same base address.
If the setting is absent, both use `ILLO_PUBLIC_URL`, as before.
A local Google sign-in still needs the final address pasted into the person's DM.
The chat setup message always shows the callback address in use.
After you change this setting, write `connect Google` and add the shown address to the Google client.
The installer already sets `ILLO_VAULT_KEY`. Keep this key outside the checkout.
The chat forms need Slack Socket Mode and the normal bot sign-in.
Apply the database migrations before this app version starts.
Migration `1610_mcp_browser_oauth.sql` separates the browser sign-in providers in the state table.

## Sign in with Slack

App sign-in at the agent door needs a public HTTPS origin. See [Agents](agents.md#operator-setup-for-app-sign-in).
The code method stays available without the Slack sign-in client.

1. On the Slack app page, open **Basic Information**.
   Copy **Client ID** and **Client Secret** from **App Credentials**.
2. Open **OAuth & Permissions → Redirect URLs**.
   Add `https://illo.example.com/oauth/agent/slack/callback` with your own public host.
   `deploy/slack/manifest.yaml` supplies that example under `oauth_config.redirect_urls`.
   It also supplies `openid` under `oauth_config.scopes.user`.
   Slack sign-in requests only `openid`. Keep it separate from the bot installation flow.
3. Store both values in the vault. The installer already sets `ILLO_VAULT_KEY`.
   Each command asks for its value with hidden input. Do not put the value in the command.

   ```sh
   docker compose --env-file "$ILLO_ENV_FILE" -f deploy/compose.yaml exec illo illo secret set slack-client-id
   docker compose --env-file "$ILLO_ENV_FILE" -f deploy/compose.yaml exec illo illo secret set slack-client-secret
   ```

   With the CLI outside Docker, use:

   ```sh
   illo secret set slack-client-id
   illo secret set slack-client-secret
   ```

4. Confirm that the Slack connection works. It records the workspace team ID for the HTTP routes.
   Add your own Slack ID to an active entry in `people.yaml`.
   Start Connect in your app, then click **Sign in with Slack** and **Allow**.

The values take effect on the next request. No new environment variable or restart is needed.
Without both values, the authorization page keeps only the pairing code.
The Slack sign-in document does not state whether this manifest change needs an app reinstall.
Each person grants the sign-in scope in the separate OpenID flow.
See [Slack's sign-in guide](https://docs.slack.dev/authentication/sign-in-with-slack/)
and [the manifest reference](https://docs.slack.dev/reference/app-manifest/).

## Run it on your laptop to try it

Run this command from the checkout:

```sh
deploy/smoke.sh
```

It builds both images and starts a separate Compose project with a random name.
It uses the `fake` provider (`ILLO_PROVIDER_FAKE=1`) on every route, no Slack connection, and no model key.
It checks both services, runs `illo doctor`, creates a temporary agent token, and calls MCP `initialize`.
It also checks a database backup and restore.
The command removes its containers, networks, volume, and temporary files when it ends.
Set `ILLO_SMOKE_PORT=18485` if port `18484` is in use.

For a persistent trial, run the normal install. Add `ILLO_PROVIDER_FAKE=1` to the brain file,
and give `brain.routes.default` the `fake` provider and the model `fake` in `illo.yaml`.
Leave both Slack tokens blank.
Then run the Compose command above to recreate the services.

## Update, backup, restore, uninstall

### Update

Keep the checkout on the branch in `ILLO_DEPLOY_BRANCH`.
The default is `main`. Set `next` while the rebuild lives there.
The updater requires a clean checkout and uses only fast-forward merges.

```sh
deploy/update.sh
deploy/status.sh
deploy/update.sh --watch 300
```

`--watch` defaults to 300 seconds if you omit the number.
A shared lock stops concurrent installs, updates, backups, and restores.
The updater builds image tags from the commit SHA and runs migrations before the restart.
Before the migration, the new images check the workspace and its routes:
`python -m brain.providers` in the new brain image gives the providers, and `illo check-routes`
in the new app image checks `illo.yaml` against them. A failure stops the update with no change.
The old services keep running.
It checks that the new app reports the new SHA.
It saves the last good SHA in `~/.local/state/illo/last-good`.
It writes results to `~/.local/state/illo/update.log`.
`ILLO_STATE_DIR` can select another state folder.
Do not delete old image tags that you need for rollback.

The updater applies the whole Compose project and removes orphan containers when the layout changes.
It recreates Postgres only when its definition changes, then waits for its health before migrations and app startup.
It saves the deploy folder of each good revision and uses that folder with the old images for rollback.
`deploy/status.sh` also uses the saved folder of the last good revision.
It never runs `down` during an update and keeps the named database volume.

If a build, migration, restart, or health check fails, the updater starts the last good images.
It never deletes the database volume.
The checkout can stay at the failed commit. The next run retries that commit.
An image rollback does not undo migrations that already committed.
Migrations must keep the old app compatible. Take a backup before a schema change.

For a user service, copy and edit the unit:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/illo-update.service ~/.config/systemd/user/
# Edit WorkingDirectory and ExecStart if the checkout is not ~/illospace.
systemctl --user daemon-reload
systemctl --user enable --now illo-update.service
```

The user service runs after login. A host administrator can enable user lingering for startup before login.
A cron entry is another option. Replace `/home/acme/illospace` with the checkout path:

```cron
@reboot /home/acme/illospace/deploy/update.sh --watch 300 >>/home/acme/illo-update-cron.log 2>&1
```

Use one watcher. Do not run both the unit and the cron entry.

### Backup

Set `ILLO_BACKUP_DIR` to an absolute folder outside the checkout.
The installer defaults to `~/.local/share/illo/backups`.
The script writes a custom-format Postgres dump with mode `600`.
It keeps the last 14 successful backups. Set `ILLO_BACKUP_KEEP` to change this count.
A failed dump does not replace or delete a good backup.

```sh
deploy/backup.sh
```

Example output:

```text
Backup: /home/acme/.local/share/illo/backups/illo-20260101T020000Z-AbCdEf.dump (42000 bytes)
```

Daily cron entry:

```cron
0 2 * * * /home/acme/illospace/deploy/backup.sh >>/home/acme/illo-backup-cron.log 2>&1
```

Copy the backup, the environment file with its vault key, and the workspace to a separate secure host.
The database dump alone cannot recover the vault without its original key.
The dump does not contain the workspace files.

### Restore

Use a fresh Postgres volume. The script refuses a database that has tables.
It never deletes an existing database.
Keep the old environment file and its vault key for the restored instance.
Use another `ILLO_PROJECT_NAME`, state folder, and local port to test a restore beside a live instance.

```sh
deploy/restore.sh /absolute/path/to/backup.dump --yes
```

The script starts Postgres, checks that the database is fresh, and stops the app and brain.
It restores the dump in one transaction.
Restore the workspace and its file owner.
For the original install, start the services with the Compose command above.
For a new install, use a new `ILLO_STATE_DIR`, then run `deploy/install.sh` to build and record the images.
Run `deploy/status.sh` and `illo doctor` after the restore.

### Uninstall

These three commands stop Illo, delete its database volume, and delete its checkout.
They use the default project name and environment path.
Back up the database first. The volume deletion cannot be undone.
The external environment file, workspace, and backups stay on disk.
Stop the update unit or cron entry first if you enabled one.
Run the commands from the parent of the checkout:

```sh
ILLO_ENV_FILE="$HOME/.config/illo/env" docker compose --env-file "$HOME/.config/illo/env" -f illospace/deploy/compose.yaml down
docker volume rm illo_postgres
rm -rf illospace
```

## Slack channel access

Add Illo to a channel in Slack, then mention it.
A channel needs no entry in `illo.yaml`.
List a channel only for coordinator mode, its own rules, or `mode: off`.
`slack.default_mode: listen` is the default. `off` accepts only listed channels.
`slack.default_rule_files` selects rule files for channels with no entry. Its default is an empty list.

The optional join notice uses the `member_joined_channel` bot event in `deploy/slack/manifest.yaml`.
For an existing install, add this event on the Slack app page under Event Subscriptions, Bot Events.
All channel features work without this event.
Illo sends the notice at most once per channel. A failed send does not block channel use.

A public channel has class `team` and supports shared search and the team brief.
For a private channel with no entry, Illo checks that its humans are exactly the active people in `people.yaml`.
For a listed private channel, set `team_members_only: true` and keep `visibility: team` for this proof.
Illo accepts its own bot and other bot users. Bots in the channel can read its messages.
Illo checks all member pages and caches the class for ten minutes.

Every other private channel has class `members`. `visibility: private` also selects this class.
Illo answers known active people who mention it there. It uses team tools and no personal data.
It does not put the channel text in shared search. A private thread read needs live proof of membership.
Tasks that people request there go to the normal team list.
A Slack error blocks the message and its index write. Illo records the missed event and a sensor error.

## Optional shell sandbox

Compose builds the sandbox with the gateway and brain. The sandbox uses no credentials.
The gateway keeps repository copies on a shared volume. See [Sandbox](sandbox.md) for settings, limits, and disable steps.

See [Task pages](pages.md) to enable browser sign-in and the task routes.
