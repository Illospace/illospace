# Install Illo

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
   The script creates `~/.config/illo/env` with mode `600`.
   It generates the database password, the vault key, and the brain secret.
   Set `ILLO_ENV_FILE` to use another absolute path outside every checkout.
   A second install keeps the existing environment file and workspace.

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
   The database is ready.
   The vault key is valid.
   Slack is optional and has no bot token.
   ```

The environment file uses literal `KEY=value` lines.
Use single quotes around values with spaces, `#`, or `$`.
Do not use shell commands, variable expansion, or multiline values in this file.
Keep secrets outside the checkout and outside the workspace.
The Compose file accepts `ILLO_ENV_FILE`; the scripts always set its absolute path.
Direct Compose commands must pass the same file with `--env-file`.

## Set up the team

1. Create a Slack app from `deploy/slack/manifest.yaml`.
   Enable Socket Mode and install the app in your Slack workspace.
   Add its bot token and app token to the environment file.
2. Edit `people.yaml` in the workspace folder.
   Replace the example people with your team members and their real Slack IDs.
   Edit `illo.yaml` for the team's channels, repositories, and model.
3. For the `claude` runtime, set `ANTHROPIC_API_KEY`.
   You can also set `ANTHROPIC_BASE_URL`.
   The runtime uses the Claude Agent SDK.
   For `openai_compat`, set `ILLO_BRAIN_RUNTIME=openai_compat`, `OPENAI_API_KEY`, and `OPENAI_BASE_URL`.
   Set `ILLO_BRAIN_MODEL` to the model name that the provider accepts.
   The `echo` runtime needs no model key and is only for tests.
4. Recreate the services after an environment change.
   Use the saved revision so Compose starts the correct image tags:

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
Publish only the routes that you need, such as `/mcp` and `/oauth/`.
Do not publish Postgres, the brain, `/brain/mcp`, or the admin reload route.

Optional GitHub and mailbox settings appear in `deploy/env.example`.
For email, set `ILLO_EMAIL_AUTHSERV_ID` to the name of the receiving server, such as `mx.google.com`.
[connectors.md](connectors.md) explains the sender check.
For a GitHub App, mount its private key from an external file into the app container.
Set `ILLO_GITHUB_APP_PRIVATE_KEY_FILE` to its container path.
Google connector credentials go into the vault through `illo secret set`.
Do not put connector secrets in the workspace.

## Run it on your laptop to try it

Run this command from the checkout:

```sh
deploy/smoke.sh
```

It builds both images and starts a separate Compose project with a random name.
It uses the `echo` brain runtime, no Slack connection, and no model key.
It checks both services, runs `illo doctor`, creates a temporary agent token, and calls MCP `initialize`.
It also checks a database backup and restore.
The command removes its containers, networks, volume, and temporary files when it ends.
Set `ILLO_SMOKE_PORT=18485` if port `18484` is in use.

For a persistent trial, run the normal install and set `ILLO_BRAIN_RUNTIME=echo` in the environment file.
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
It checks that the new app reports the new SHA.
It saves the last good SHA in `~/.local/state/illo/last-good`.
It writes results to `~/.local/state/illo/update.log`.
`ILLO_STATE_DIR` can select another state folder.
Do not delete old image tags that you need for rollback.

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
