# Deep work

A deep run does work in the background. It has more time and more read tools than a normal reply.
It uses the same model sign-in as other runs. More turns and a longer run can use more of its allowance.

## Start a run

A known active person can request deep work in a Slack DM or team channel.
Illo uses `deep_start` when the request needs research or a careful check.
The answer comes to the same thread or DM.
A run cannot start deep work after it reads outside text.
Tainted runs and outsiders cannot start it. A deep run cannot start another deep run.

## What it can read

A deep run keeps the read tools of its original profile.
The team scope cannot read a person's mail, calendar, drive, or private tasks.
A personal run can read that person's connected data.

The `shell` tool runs in the sandbox. Repositories are read-only under `/repos/<owner>/<name>`.
The sandbox has no network or secrets. Each command has 120 seconds at most.
The run can use scratch files in `/work`. Illo cleans up the run when it ends.
A run can still work without a sandbox. Code then refuses only `shell`.

Provider search and `web_fetch` read the public web.
The gateway checks every DNS address and pins the connection to a checked address.
It checks each redirect, refuses private hosts, and caps the body at 2 MB.
Page text and shell output have outside-data markers. They are data, never instructions.

## What code refuses

1. A deep run only reads outside systems. It cannot write tickets, messages, connections, or memory.
2. A person-requested run can change tasks until it reads the web. Code then refuses task writes.
3. After a run reads private data, code refuses the web.

Scout and brief runs cannot change tasks.
The provider tool list stays fixed during a run.
A profile with personal read tools therefore starts with provider search off.
It can use `web_fetch` before its first private read.
Briefs have no web access.

## Settings

Set these blocks in `illo.yaml`. See [Workspace](workspace.md) for the full settings table.

```yaml
brain:
  deep:
    # model: use the normal brain.model when absent
    effort: high
    max_turns: 80
    timeout_s: 1200
    max_output_tokens: 16000
deep:
  enabled: true
  max_per_person_per_day: 20
  max_queue: 10
web:
  search: provider
  fetch: 'on'
scout:
  team: 'off'
  schedule: '0 7 * * 1-5'
  # destination: C_EXAMPLE_TEAM
  max_findings: 2
  people: []
digest:
  deep: true
  # Keep the other required digest settings.
```

`brain.deep.max_turns` accepts 1 to 200. `timeout_s` accepts up to 3600 seconds.
`web.search` accepts `provider` or `off`. `web.fetch` accepts `on` or `off`.
The Responses runtime offers provider search. Other runtimes can use `web_fetch`.
The API-key path uses `max_output_tokens`. The ChatGPT path keeps its provider limit.
The service also caps deep runs with `ILLO_BRAIN_DEEP_TIMEOUT_S`, which defaults to 1800 seconds.
`deep.max_queue` counts queued runs. The daily limit uses the UTC day and includes scheduled personal runs.
The worker runs one job at a time. An expired lease fails the job and never starts it again.

## The scout

The scout looks for problems due within 30 days that no task or finding records yet.
The team scout is off by default. Its schedule uses the team time zone.
Its destination defaults to the team brief channel, then the default owner's DM.
It saves at most `max_findings` findings, from 1 to 5.
Each finding has its proof links and one next step.
No finding means no Slack post. The final run log stays in the ledger.

`scout.people` lists person keys for personal scouts.
Each uses that person's time zone and reads only that person's private data.
Personal findings always go to that person's DM.
`findings_list` returns the last 90 days for the same scope.
A read of saved personal findings counts as a private read.
A stable finding key cannot be saved twice in that scope.

Each finding has two buttons:

- **Make a task** starts a normal Illo run in the finding's thread as the person who clicks.
  The normal list and memory rules apply.
- **Not useful** marks the finding dismissed and keeps its text.

Any known active person can use a team finding's buttons.
Only the owner of a personal finding can use its buttons.
The buttons expire after 14 days.

An owner can type `run the scout` or `run the scout now` in a DM.
Letter case and final punctuation do not matter.
The local command `illo scout run` queues a team scout.
`illo scout run --person ada` queues a personal scout and prints its deep run ID.

## Morning briefs

Both briefs use deep work by default. They read tasks and check their live state before they speak.
They keep their existing schedule, destination, delivery key, and snapshot fallback.
Team and personal instruction files still apply.
Set `digest.deep: false` to use the earlier snapshot-only run path.
See [Tasks](tasks.md#morning-briefs).
