# Sandbox

The sandbox runs shell commands for background model runs. The model gets no GitHub token or service secret.
The gateway keeps the repository copies current. The sandbox reads these copies at `/repos`.
Commands can use `git`, `rg`, `jq`, `less`, `file`, `find`, `diff`, Bash, and Python 3.

## Network and files

The sandbox joins only `sandboxnet`, an internal Docker network. Only the gateway also joins this network.
The sandbox has no published port. It cannot reach the internet, GitHub, the database network, or the brain network.
It can reach the gateway on `sandboxnet`. Gateway routes still apply their own access checks.
The service has no authentication header. Keep this network private. Never publish port 8600.

The service uses UID and GID **10002**. The gateway uses UID and GID 10001 by default.
The sandbox reads the `repos` volume at `/repos`. The gateway mounts that volume with write access.
The image gives the gateway ownership of the empty volume on first use.
If you change the gateway UID, give that UID write access to this volume before use.

The root filesystem is read-only. `/work` is a temporary filesystem with a 2 GB limit.
Each run uses `/work/<run_id>` as its home and default command folder.
`/tmp` has a 256 MB limit. Both mounts use `noexec`, `nosuid`, and `nodev`.
Use `python3 script.py` or `bash script.sh` to run a script. Direct script execution is blocked.
Runs share a service user and the same temporary filesystems. Run folders are not separate security boundaries.
Do not put credentials in commands or run folders.

## API

The gateway calls `http://sandbox:8600`. All requests and replies use JSON.

- `GET /healthz` returns `{"status":"ok"}`.
- `POST /exec` accepts `run_id`, `command`, optional `timeout_s`, and optional `cwd`.
- `POST /cleanup` accepts `run_id` and deletes the run folder. It returns `{"ok":true}`.

`run_id` contains 1 to 64 letters, digits, underscores, or hyphens.
The command contains at most 8000 characters. The timeout defaults to 30 seconds and allows 1 to 120 seconds.
The service runs `bash -lc <command>` in a new process group.
The folder must be an absolute path inside `/repos` or the current run folder.
The service rejects `..` and checks the resolved path. A symlink cannot escape these roots.
Bad input gets HTTP 422. A cleanup during an active command gets HTTP 409.

An exec reply contains `exit_code`, `stdout`, `stderr`, `truncated`, `timed_out`, and `duration_ms`.
Each output stream keeps at most 64,000 bytes, before UTF-8 decoding with replacement.
Large output keeps its head and tail with an `[output cut]` mark.
The service never logs command output. It does not log commands either.
A timeout kills the whole process group with SIGKILL. The service also kills remaining group members after command exit.
A command can still fail when the container reaches a shared resource limit.

The child environment contains only these initial values:
`HOME`, `PATH`, `LANG=C.UTF-8`, `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_GLOBAL`, and `PAGER=cat`.
Bash can add its own shell variables. The read-only Git config sets `safe.directory=*` and `core.pager=cat`.
No variable from the service environment passes to the command.

## Limits

- The service runs at most two commands. A third waits up to 10 seconds, then gets HTTP 429.
- CPU time per command is the timeout rounded up, plus 5 seconds.
- Address space per command defaults to 8 GB. The container memory limit of 3 GB is the real limit. `ILLO_SANDBOX_MEMORY_BYTES` can change this limit.
- File size per command defaults to 256 MB. `ILLO_SANDBOX_FILE_BYTES` can change this limit.
- Each command allows 256 open files and 128 processes for the service user.
- The container allows 256 processes, 3 GB of memory, and two CPUs.
- The container drops all capabilities and blocks new privileges.

The service deletes run folders older than six hours at startup and every 30 minutes.
It skips active runs. Cleanup removes temporary work; it does not change repository copies.

## Repository copies

Set `ILLO_REPOS_DIR=/repos` on the gateway. GitHub must also be configured.
The optional `sandbox` block in `illo.yaml` controls repositories, branches, depth, interval, and size.
See [Workspace settings](workspace.md#repository-mirrors-for-the-sandbox).

`/repos/<owner>/<name>` holds a shallow checkout of the default branch.
Read `/repos/README.txt` first. It lists each copy, its default branch, extra refs, and last sync time.
Use `git show origin/staging:path` or `git grep pattern origin/staging` to read an extra branch.
An absent extra branch is skipped. Illo deletes refs that no longer belong to the settings.

The gateway syncs one repository at a time, with low CPU and disk priority when available.
Each repository has a ten-minute timeout. A database lock prevents concurrent sync passes across gateway processes.
A deep run can call `ensure_fresh(ctx)` before it reads the copies. It waits at most five minutes for a current pass.
The last clean pass and each repository result stay in `kv`. The sensor `sandbox:repos` reports failed passes.
`illo doctor` reports sandbox access and the count and age of the copies.

The gateway gets the token from the existing GitHub client. This includes GitHub App installation tokens.
Only the Git child environment gets the token as an extra HTTP header.
The remote URL, command arguments, Git config, README, and error text contain no token.
Illo checks the Git config after a sync. It deletes a failed copy and reports an access error.
It also deletes removed repositories. A copy above `max_repo_mb` is deleted and skipped until settings change.
The copies are disposable. Illo resets local changes on each pass.

## Install and disable

The installer and updater build the sandbox from this repository with the current revision tag and image label.
No separate image push or manual build is required. The updater checks the sandbox through the gateway network.
The restore script stops the sandbox before a database restore.

To disable shell access, do not set `ILLO_SANDBOX_URL`, or remove the sandbox service.
The standard compose file sets this URL. Remove that environment entry when you disable the service.
To disable repository sync, unset `ILLO_REPOS_DIR`, remove GitHub configuration, or set `sandbox.repositories: []`.
Repository sync does not require shell access.

Tests can set `ILLO_SANDBOX_WORK_ROOT` and `ILLO_SANDBOX_REPOS_ROOT` to temporary folders.
Production uses `/work` and `/repos`.
