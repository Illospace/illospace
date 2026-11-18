# Task pages

Each task has a page at `/t/{id}` when pages are on.
The page shows the next action, the work, the proof, the brief, and the history.
It uses the same visibility rules as task tools. Only the owner sees a personal task.

## Sign in

1. Open the task link from Slack.
2. Send the displayed `connect XXXX-XXXX` line to Illo in a Slack direct message.
3. Read the card. Press Approve only if you started this sign-in.
4. Return to the same browser. The page continues after approval.

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
Outside and tainted runs cannot read or write pages.
A coding agent uses `task_page` through MCP. It can write only a task it holds, under the `task_write` holder rule.

A reviewer can Approve or Send back. Send back requires a reason.
Failed Done checks show a Confirm anyway form. This form requires a reason.
An open task also accepts a message to its holder.
These actions use the same ledger functions and delivery paths as Slack.
If the work changes after you open the page, read it again before you act.

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

Expose `/t/` and `/web/` through the gateway. Use the team's private network first.
Keep the brain, Postgres, and admin routes private.
When pages are off, these routes return 404 and the page tools disappear.
Slack task links keep their previous destinations.
