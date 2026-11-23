# Task pages

Each task has a page at `/t/{id}` when pages are on.
The page shows the next action, the work, the proof, the brief, and the history.
It uses the same visibility rules as task tools. Only the owner sees a personal task.

## Sign in

The fast way starts in Slack:

1. Press **Open the page** on a task or request card from Illo.
2. Illo shows a link in a window that only you see. Open the link.
3. Press **Continue** in the browser.

Slack proves who pressed the button, so this way needs no code.
The link works once and expires after 10 minutes. A link preview uses nothing.
In a browser with no session, only **Continue** uses the link.
A link never changes the person of a browser that is signed in. If that person owns the link, the link ends.
Illo's own logs drop each sign-in request that has a query. Set the same rule in a reverse proxy.
If Slack does not show the window in time, Illo ends the link and asks for a new press.
Illo gives a link only for a task or request that the person can see. Each person gets 20 links per hour.

The other way starts in the browser:

1. Open the task link from Slack.
2. Press **Get a code**. Only this button makes a sign-in request. A link preview makes none.
3. Send the displayed `connect XXXX-XXXX` line to Illo in a Slack direct message.
4. Read the card. Press Approve only if you started this sign-in.
5. Return to the same browser. The page continues after approval.

The code works once and expires after 10 minutes.
Another browser cannot use your approval. The browser session lasts 30 days.
An inactive person cannot sign in or use a session. Sign out ends the session.
The browser uses no password or Slack OAuth grant.

## Page blocks

A model writes the whole page in one call. A page has at most 12 blocks.
A new write replaces the old blocks. The brief stays separate.

| Kind | Content |
|---|---|
| `text` | An optional title and text with basic Markdown: headings, lists with one more level, tables, code, quotes, links. |
| `preview` | The author name, label, plain text, and an optional image. It shows work as its reader sees it. |
| `image` | An image from a repository or an HTTPS address, a caption, and alternative text. |
| `checks` | Up to 12 claims with `yes`, `no`, or `open`, a source URL, and a check time. |
| `bars` | Up to 12 labels and finite values of zero or more. Each row can have a unit. |

Text and preview text have a limit of 6,000 characters per block.
The server escapes all text. It accepts no HTML or script from a model.
The page warns when the result is newer than the blocks.

An image can name a file in a configured repository mirror with `repo`, `ref`, and `path`.
The server resolves its reference to a fixed commit when it writes the page.
The file must be PNG, JPEG, WebP, or GIF, and no larger than 8 MB.
The browser reads the image through the signed-in task route.
If the mirror loses the commit, the page shows that the picture is unavailable.

An image can also use `{"url": "https://example.com/picture.png"}`. A preview can use either image form.
The address has at most 2,000 characters. It has no credentials or control characters.
`pages.outside_images` is off by default. Illo makes no request when it is off.
The page shows a picture link and the caption. The call returns a note for each such block.
That choice stays with the block if the setting changes.

When `pages.outside_images` is on, Illo gets one copy before it writes the page or ask.
Use a public HTTPS address on port 443. Give the final address, with no redirect.
Illo checks every DNS answer and pins a public answer for the request.
A fetch has a total limit of 10 seconds. Each file has a limit of 5 MB and 8,000 pixels per side.
Illo reads the file header. It accepts PNG, JPEG, WebP, and GIF only.
A call gets pictures in order. The process gets at most two pictures at once.
If a picture fails, the whole call fails and writes nothing.
Only an agent at the agent door (`task_page`, `ask_create`) can give a picture address that Illo gets.
A page that Illo's own model writes shows such an address as a link. That model reads text that Illo does not trust.

The picture store has a total limit of 500 MB. A task page write replaces its stored pictures.
The browser reads a stored picture through a signed-in file route. It makes no outside request.
Each day, Illo deletes pictures of closed asks created more than 90 days ago.
It also deletes pictures of done or cancelled tasks with no change for 90 days.
The page then shows “The picture is not available.”

## Tools and decisions

`page_read` returns the blocks, both result versions, and the page URL.
`page_write` replaces the blocks of a visible task.
Person DMs, routines, and agent requests can write pages. Team channels can only read pages.
Deep requests can write pages. A wait check can write only its own task page.
When work comes back for a person to review, Illo starts one look at the work that fills the page. The look is a wait check, so it needs `tasks.wait_check`, deep work, and a team task. It sends no message of its own.
At most two wait checks run at once, so a look can wait for a later tick.
The review card in Slack waits for this look, for 6 minutes at most. When the look ends with a message, the card has Illo's words on what to review. Its first button opens the page. When the look fails, has no message, or is late, the card has the holder's words. A late look changes the card and sends one message that the page is ready.
`page_read` returns `stale: true` when the work came back or the result changed after the page was written. The page says so too.
Outside and tainted runs cannot read or write pages.
A coding agent uses `task_page` through MCP. It can write only a task it holds, under the `task_write` holder rule.
When an agent changes the content of the page of a task that a person must review, Illo tells that person in Slack.
A write that changes no block sends nothing. A new check time alone is no change. A picture at a new commit is a change.
A notice longer than 3,000 characters is plain text with the link and no button.
The message has the page link and the **Open the page** button. Reminders have the same button.

A reviewer can Approve or Send back. Send back requires a reason.
Failed Done checks show a Confirm anyway form. This form requires a reason.
An open task also accepts a message to its holder.
These actions use the same ledger functions and delivery paths as Slack.
If the work changes after you open the page, read it again before you act.

## Request pages

An agent's ask has a request page at `/web/a/{id}` when pages are on.
`ask_create` returns this address in `page_url`. It returns null when pages are off.
`card_text` contains the card's first line and question as plain text.
The page shows who asks, the question, the blocks, the proof, every link, and the options.
`ask_create` accepts the same five block kinds, with at most 12 blocks. Blocks appear before the proof.
The request file route is `/web/a/{id}/file/{position}`. Only the two participants can read its pictures.
Only the active owner and requester can open it. A workspace owner has no extra access.
The owner can choose an option or give another answer of 1 to 4,000 characters.
The requester can read the page but cannot answer. Slack and the page use the same answer service.
A changed request requires a fresh form. A closed request needs no answer.

Put a long brief in `proof.text` or `proof.body`. Other named entries get their own sections.
The page shows strings, numbers, booleans, nested lists, and named values. Picture addresses remain links.
All entries in `proof.links` appear. The routing entry `area` stays hidden.
The proof has at most 40 values, 20,000 bytes as compact JSON, and three levels. The proof object is level one.
A string `saved:<number>` anywhere in the proof names a saved item.
Both participants must have access when the agent creates the ask.
Each page read checks the reader's access again. An unavailable item shows no title or text.
The ask stores the reference, never a copy of the saved text.

When pages are off, the agent's Slack card shows the complete proof and saved text.
A request with more than 45 card blocks or 30,000 characters is refused before an ask is written.
Illo's own asks and collector asks keep their short cards.
Only a decision or permission ask can offer a standing answer.

One message serves one request. A new ask can reuse the card of a recently cancelled ask.
The token and owner must match. The cancel must be less than 10 minutes old.
The new card replaces the old card. Old buttons stop working, and thread answers go to the new ask.
An answered ask keeps its message and its answer.

## Setting and access

Pages are off by default. Add this block to `illo.yaml`:

```yaml
pages:
  enabled: true
  outside_images: false
  url: https://illo.example.com
```

The base address is `pages.url`, then `connect.public_url`, then `ILLO_PUBLIC_URL`.
Use HTTPS. HTTP is allowed only on a loopback host.
For local HTTP tests, set `ILLO_WEB_ALLOW_HTTP=true` so the browser can send the cookie.
Do not set this option on a shared host.

Expose `/t/` and `/web/` through the gateway. Request pages are under `/web/`. Use the team's private network first.
Keep the brain, Postgres, and admin routes private.
When pages are off, these routes return 404 and the page tools disappear.
Slack task links keep their previous destinations.

## Task progress

An open task with an agent as holder shows a progress frame above its blocks.
The frame reads `/t/{id}/progress` each 15 seconds while the task is `todo` or `working`.
The main page does not refresh. A person can keep a message open.
The frame uses the same session and task access checks as the task page.
Without access, the frame returns a plain 404.

Code computes the stages from current facts, with no model call:

- `Waits for the agent`: the task is open, its holder is an agent, and no task lane is live.
- `Works`: a task lane is live. The existing `task:lane-live:` key defines live.
- `Pull request`: the task or a task lane has a pull request link. The task has not returned.
- `Your review` or `Review`: the task state is `review`. The first label names the person who must act.
- `Done`: the task state is `done`.

Later stages take priority. A task without a repository or pull request link omits the pull request stage.
Lane reports supply the activity, the agent's own steps, and the count of changed files.
A branch link supplies the branch. Step text stays escaped data, with hidden comments removed.
Illo never sends this step text to GitHub or Slack.
A lane without a signal for more than 10 minutes shows the time without a signal.
When the task leaves work, the frame stops its refresh and links back to the task page.

Illo keeps a task-to-lane key when the existing lane handler finds a match.
This key preserves the link after a lane stops. It does not change the rule for a live lane.
Task readers cannot read progress text from another person's private lane.

## Board

Open `/web/board` after sign-in. The header shows the date in your time zone.
Each task and request page also has a **Board** link.
The board has three columns, with at most 30 frames in each column:

- **Needs you** shows tasks that wait for you and open requests that you must answer. The oldest wait comes first.
  Each frame has the page blocks and the same forms as its page.
  A request shows the first three proof entries in short form. Open its page for the complete proof.
- **In work** shows visible tasks in `todo` or `working` that do not wait for you. The latest change comes first.
  An agent frame shows its stage, current action, and step count. A person frame names its holder.
- **Done today** shows visible tasks closed in the last 24 hours. The latest close comes first.
  Each frame names the person who approved it, or gives the close time.

Each title opens its own page. A frame named in the address has an ink outline.
Forms from the board return to that frame. Access stays the same as on each page.
The board states how many more frames the task list in Slack holds.

With a script, drag the paper or a column title to pan. The wheel and arrow keys also pan.
Use **+**, **−**, **Fit**, Ctrl or Command with the wheel, or a touch pinch to zoom.
The zoom range is 0.4 to 1.5. A frame cannot move on its own.
The browser keeps the view in session storage across a form post.
Every 30 seconds, a visible tab reads the two work columns again when no form has focus or typed text.
The **Needs you** column stays intact. A link asks you to read the board again when its frame IDs change.
Below 48rem, the board stays a normal page with one column below the other.
Without a script, all columns scroll as a normal page and all forms work.
The board has no animated moves, including with reduced motion.

Set `pages.open: board` to send people to a frame that is currently in their **Needs you** column.
This applies to Slack page buttons, task links, and the morning brief.
Other links keep the page address. The default, `pages.open: page`, keeps the current links.
Agent results always give the task or request page address.
