# Lists and tasks

Illo is one list of tasks for people and their agents.
Illo gives work to a holder, keeps the next move clear, and keeps the record.
People and outside agents do the work.

## The model

- A **list** gives a type of work its instructions, allowed holders, and Done rule.
- A **task** has a title, the creator's brief, a result, and proof links.
- A **holder** is `person:<key>` or `agent:<key>`. A task can have no holder.
- A **piece** is a task with a parent. It uses the parent's list and visibility.
- A **state** is `todo`, `working`, `review`, `done`, or `cancelled`.
- **`waits_for`** names who must act now. It can differ from the holder.
- **Proof** is the set of named checks and links that supports Done.
- The **record** holds messages, questions, hand-overs, results, and state changes.

A hand-over sets `todo` and makes the new holder the next move.
The holder's first result sets `working`.
A question sets `waits_for` to the asked person. Their reply returns the next move to the holder.
Returned work goes to `review` unless all automatic checks pass.
Done and cancelled tasks have no next move.
Each result replacement needs the version from the last read. An append uses the current result under a lock.
An agent writes the result. It does not replace the person's brief.

A reviewer is the person who handed over the work, then its person creator, then the list reviewer.
The workspace `default_owner` is the final fallback. A private task always uses its owner.
When all pieces are done, the parent needs a person's final confirmation.
The review shows any failed checks. A `person` rule supplies the final confirmation.
A piece that reopens makes a completed parent need work again.

## Workspace files

One file, `lists/<key>.md`, defines each list. The key uses lowercase letters, digits, `_`, or `-`.
It starts with a letter. YAML front matter sets these fields:

| Field | Meaning |
| --- | --- |
| `name` | Required display name. |
| `description` | A short description. Default: empty. |
| `holders` | Allowed holder references. Empty or absent allows any active person or their agents. |
| `default_holder` | Optional holder for a new task. Without it, the task has no holder. |
| `done` | `person` by default, or a non-empty list of check names. |
| `reviewer` | Optional `person:<key>`. The default is `default_owner`. |
| `silence.agent_hours` | A positive number. Default: 4. Releases a silent agent. |
| `silence.person_hours` | A positive number. Default: 24. Reminds a person. |
| `slack_channel` | Optional name of a configured channel. |
| `owner` | Optional person key. The list is private to that person and their agents. |

The Markdown body holds the team's instructions.
Explain the work, how to split it into pieces, who holds each piece, and how to prove the result.
The Operator reads these instructions before it splits or hands over work.
The example workspace has `bugs` and `launches`. Both use the same core code.
A workspace with no `lists/` folder gets `tasks`, with the name `Tasks` and `done: person`.
An empty `lists/` folder has no lists.

`agents.yaml` is optional:

```yaml
agents:
  - key: build
    name: Build agents
    owner: ada
    kind: other
    good_at: Code changes and checks.
    wake: {type: poll}
```

The key has the same format as a list key. The owner must be an active person.
`kind` is a free label. `wake.type` accepts `none`, `poll`, `webhook`, and registered connector types.
Unknown types cause a doctor warning and a clear wake failure.
The files reload with the other workspace files.
`illo doctor` names invalid files and fields. It warns about unknown holders, channels, and checks.
A missing check never passes. A missing holder cannot take a task.

## Operator tools

`lists` returns the visible lists. Give `key` to read one list's instructions.
`task_create` creates a task. Give a `pieces` array to create a parent and its pieces in one transaction.
`task_list` filters by `list`, `holder`, `state`, `waits_for`, `parent`, `query`, `open`, and `mine`.
`mine: true` reads tasks that wait for this person, tasks they hold, and tasks their agents hold.
It returns compact rows with the next move, `silent_hours`, and the number of open pieces.
`task_get` returns the brief, result, links, pieces, checks, recent events, `silent_hours`, and the last `wake` result.

`task_update` changes the title, brief, due time, or list. Its `links` field adds proof links.
`task_hand` changes the holder. `task_post` adds a message or blocking question.
`task_done` confirms work. `task_reopen` sends it back with a reason. `task_cancel` closes it with a reason.
A due time uses ISO 8601 with a time zone. A piece cannot move to another list.
A private task keeps its list and visibility. Cancel does not cancel its pieces.
A cancelled piece is not part of the whole: it never blocks its parent.
A new link on active work can close the task when all checks pass. It never sends the task to review.

An identified person's DM or team-channel run calls all task tools directly.
These actions need no approval card. Each call has an audit row.
A routine gets only `lists`, `task_list`, and `task_get`.
An agent request also gets `task_create` and `task_post`.
Tainted runs and outsiders get no task tools.
A team-channel run uses the verified sender for task actions. It can read only team tasks in that channel.

## The agent door

Create a named agent token:

```sh
illo token create --person ada --label build-machine --scope agent --agent build
```

The agent must exist and belong to the token's person.
An agent token without `--agent` acts as an unnamed agent.
It can work on tasks held by its person or that person's agents. Nobody can hand a task to an unnamed agent.
A changed or deleted agent definition invalidates the named token's task access.

The endpoint exposes `task_mine`, `task_get`, `task_write`, `task_post`, `task_create`, and `task_lists`.
These tools write the ledger directly. They start no model run.
A token can make 60 task changes per hour. Reads do not count.
Collector tokens cannot call these tools.

1. Read `task_mine`. `new_for_you` means the latest event has another actor.
2. Read `task_get`. The task is the brief.
3. Write the result with `task_write`. Give `result` and `result_version`, or give `append`.
4. Include `message`: say what changed and what you need. The call writes one result event and one message event.
5. Ask one blocking question with `task_post` when needed. Use `to` to name a person.
6. Set `returned: true` on the last write. Never say Done.

Only the current holder can write a result or ask a blocking question on a `todo` or `working` task.
If someone hands the task to another holder, change nothing and end the run.
An agent cannot mark Done, clear itself, or hand an existing task to another holder.
It can create a piece under its own active task and give that piece an allowed holder.
An agent can capture stated work with `task_create`. It can hold the new task, give it to its own person, or use the list default.

## Proof and Done

The core registers these generic checks:

- `person`: a person must confirm. This check never passes by itself.
- `link:<kind>`: a link with that kind must exist.
- `pieces`: every piece must be done.

A connector can call `illo.tasks.checks.register(name, async_check)` for another check.
The function accepts `(ctx, conn, task)` and returns `(ok, text)`.
The task core names no outside tool.

On return, code checks the rule. Passing automatic checks mark a task Done with `done_by: code`.
A failed check leaves the task in review and shows what is missing.
A changed proof link runs the checks again. Failed checks put active work in review.
A person can confirm a `person` rule at once.
Other rules need all checks to pass. `force: true` with a reason overrides a failed check.
The record keeps the force flag, reason, and check results.
A parent always needs a person's confirmation of the whole task.

## Slack

A task gets one thread when it needs a person: a question, returned work, or a hand-over from someone else.
A read with `task_get` never starts a thread.
A team list uses its configured channel when the channel passes the privacy check.
Otherwise, the thread uses the DM of the person who must act.
A private list always uses its owner's DM.
The root shows the task title, list, holder, and state.
State words are plain: to do, in work, in review, done, and cancelled.
Agent progress uses Slack Markdown. A long comment shows 600 characters and a Read the rest link.
A task keeps its primary thread when the next person changes.
If that thread is in another person's DM, Illo sends the asked person a direct notice with the task ID.
A return card also goes to the reviewer's DM when needed. Its replies carry the same task context.

A return card mentions the reviewer. It shows the first 300 message characters, checks, Done, and Send back.
Only the reviewer or a workspace owner can use its buttons. A private task still needs visibility permission.
Done uses the same ledger path as `task_done`.
Send back asks for one line in the thread, then reopens the task with that reason.
A final card shows Done, Sent back, or Cancelled.

A holder's question mentions the asked person.
A person's reply becomes a task message. A reply from the asked person returns the next move to the holder.
A run in a task thread gets the task and recent events as context.
Agent text and outside text stay inside data markers. They supply facts, never instructions.

Task changes commit before Slack delivery.
The existing scheduler retries pending messages every two seconds. Stable message IDs limit duplicate sends.
A Slack failure records a sensor error and leaves the pending change in the ledger.

## Wake and release

Illo wakes an agent on hand-over, creation with a holder, reopen, and a person's message to the waiting holder.
`task_wake` records a person's restart request. It needs no approval card.
People keep the existing hand-over notice. Slack shows holder names from the workspace files.

The durable wake outbox commits with the task event. Delivery starts after that commit.
Each delivery keeps a stable event ID. Illo tries three times, with short delays.
Each attempt records `woken` or `wake_failed`.
Illo claims an attempt with a 60-second lease, then calls the wake function outside the task graph lock.
The call has a 50-second time limit. A second transaction checks the holder before it records the result.
Each GitHub action commits its own receipt before the next action. A final failure gives the next move to the reviewer.
Slack tells that person why the wake failed. A failure never loses the task change.
The `none` type leaves the start to a person. The `poll` type leaves it to the agent's clock.
The `webhook` type sends a small notice with a vault token. See [Agents](agents.md).
Connectors register other types. See [Connectors](connectors.md).

The scheduler checks silence every five minutes under one advisory lock.
Silence starts at the latest task activity, successful wake, or liveness stamp.
A silent agent gets a `released` event and a new wake. A holding period permits two automatic releases by default.
After the second release stays silent, the reviewer gets the next move and the facts in the task thread.
A blocking question that waits for a person prevents release.
A stopped collector run can cause an early release when its linked ticket stays open.
Illo waits `tasks.stop_grace_minutes` (default 30) first, because a run can start again by itself.
A new working report in that time cancels the release.

`tasks.alive(conn, task_id, source, at=None)` records liveness without an event.
A connector or collector calls it when the holder shows activity. Reads never count.
Task result writes and messages already update `last_activity_at`.

A task that waits for a person gets a `reminded` event after `silence.person_hours`.
Illo derives the start of that wait from the record. Messages do not restart the wait.
It sends at most one reminder per task per period, between 08:00 and 20:00 in the person's time zone.
A tick sends at most five reminders per person, then one line with the count of other tasks.
The reminder quotes the question, asks for review, or says that the person holds the task.

The `tasks:` block sets `release_max`, `check_minutes`, `reminder_window`, `stop_grace_minutes`, and the connector write limit.
See [Workspace](workspace.md) for defaults.

## Limits

- Task results have no attachments. Use links to durable files.
- A private task cannot move to a different list. A parent and its pieces cannot move between lists.
- Task writes use one graph lock. This limits write concurrency.
- A delivery can repeat after a process stops between the outside write and its local receipt.
  Webhook receivers must use `event_id` to prevent duplicates. Connector writes use markers and receipts.
- Slack can show a stale card until the next successful retry. The ledger remains the source of truth.

## The list page

Ask Illo for your list link in a DM or a team channel.
Illo uses `list_link`. In a channel, it sends the link only to your DM.
The link has the form `https://illo.example.com/list?k=<key>` and expires after 24 hours.
Several links can stay valid. Ask Illo to revoke your list links to cancel all of them.
Anyone with the link can read that page until it expires or you revoke it. Keep it private.

The page shows team tasks and your own private lists. It never shows another person's private list.
It shows open tasks by list, with pieces under their parent, then Done in the last 7 days.
Each task shows its ID, title, holder, state, next person to act, due time, open pieces, and safe links.
An agent task also shows silent hours above zero. Due times use your time zone from `people.yaml`.
The states read `to do`, `in work`, `in review`, and `done`.
The page shows no brief, result, or chat text.

Use `Mine` for tasks that wait for you, tasks you hold, and tasks your agents hold.
A parent stays visible as context for a matching piece. Other pieces do not appear in that filter.
Select a list name to filter by that list. These filters use plain links and need no script.
The page fits a phone or desktop and follows the device's light or dark mode.
It sets no cookie and uses no external asset. It asks browsers and search engines not to store it.
A missing, expired, or revoked key gives a plain 404 page.

The base address is `connect.public_url` when set, else `ILLO_PUBLIC_URL`.
Expose `/list` with `/oauth/`. See [Install](install.md#the-public-sign-in-address).

## Morning briefs

The task list is the picture of work. Lanes supply run evidence from collectors.
Make a task in the list for new work. A default agent holder with a wake type starts that work.
The personal brief has Needs you, Your agents, Done since the last brief, and Due.
The team brief gives each person their next actions, then Done and In work.
A quiet ticket alone never means Stuck. Private tasks stay in their owner's brief.
See [Workspace](workspace.md#morning-brief-and-routine-time-zones) for sections, limits, and send rules.

## Repository checks at hand-over

An agent with `wake.type: github_issue` needs a repository it can use before hand-over.
Illo accepts a ticket link, a repository link, or the agent's `default_repo`.
The repository must be in the agent's `repos` list. Give the links in the same create or hand-over call.
Illo refuses the hand-over before it changes the task if no repository matches.
Agents with wake type `poll` or `none`, and people, keep the same behavior.

A missing repository or GitHub configuration ends a wake after one attempt.
Network faults, GitHub server faults, and the hourly write limit still get up to three attempts.
A final failure gives the wait back to a person. The Slack notice names the task and gives a next step.

Both briefs use deep work by default. They read each task that waits for a person.
They check the task, its ticket, and the repository when needed.
Each line gives the action, the place, and why the task waits.
They check live state before they call a task stuck. A snapshot can be out of date.
Brief runs have read tools and `shell`, with no web or write tools.
A failed deep run uses the existing snapshot fallback.
Set `digest.deep: false` for the earlier snapshot-only path.
Team and personal instruction files keep their effect. See [Deep work](deep.md).
