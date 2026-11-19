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
| `text` | An optional title and text with basic Markdown. |
| `preview` | The author name, label, plain text, and an optional image. It shows work as its reader sees it. |
| `image` | An image, caption, and alternative text. |
| `checks` | Up to 12 claims with `yes`, `no`, or `open`, a source URL, and a check time. |
| `bars` | Up to 12 labels and finite values of zero or more. Each row can have a unit. |

Text and preview text have a limit of 6,000 characters per block.
The server escapes all text. It accepts no HTML or script from a model.
The page warns when the result is newer than the blocks.

An image must be a file in a configured repository mirror.
The server resolves its reference to a fixed commit when it writes the page.
The file must be PNG, JPEG, WebP, or GIF, and no larger than 8 MB.
The browser reads the image through the signed-in task route. The page fetches no outside image.
If the mirror loses the commit, the page shows that the picture is unavailable.

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

An agent's ask has a request page at `/a/{id}` when pages are on.
`ask_create` returns this address in `page_url`. It returns null when pages are off.
`card_text` contains the card's first line and question as plain text.
The page shows who asks, the question, the proof, every link, and the options.
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
  url: https://illo.example.com
```

The base address is `pages.url`, then `connect.public_url`, then `ILLO_PUBLIC_URL`.
Use HTTPS. HTTP is allowed only on a loopback host.
For local HTTP tests, set `ILLO_WEB_ALLOW_HTTP=true` so the browser can send the cookie.
Do not set this option on a shared host.

Expose `/t/`, `/a/`, and `/web/` through the gateway. Use the team's private network first.
Keep the brain, Postgres, and admin routes private.
When pages are off, these routes return 404 and the page tools disappear.
Slack task links keep their previous destinations.
