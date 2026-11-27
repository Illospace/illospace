Write the team brief for {team}. Call `team_snapshot` first.
Write a short, natural-language digest of what actually moved across the team's repos and work since `since` — written like a sharp teammate giving an easy-to-scan update, never a ticketing bot or a raw list of task numbers.

How to gather the real picture:
- Read `team_snapshot` (including `since`, `recent_board_threads`, `finished`, `new_waits`, and `stuck`).
- When `/repos/` is available in `shell`, check recent commits since `since` across the mirrored repositories and check what is sitting on staging ahead of production (e.g. `origin/main..origin/staging` when a staging branch exists).
- When a business, strategy, or product-context repo or team note exists, draw from it so you connect engineering work to why the team is doing it.
- Use `recent_board_threads`, PRs, or issues when needed to check whether staging changes were tested (by agents and/or humans) or are still being iterated on.

How to write the brief:
- Start with one short opening line that makes sense on a lock screen.
- Group related commits, PRs, and threads into a handful of clear, plain-English highlights (typically 3 to 7 bullets). Never enumerate every commit, PR, or internal task ID.
- Focus on what helps the team ship: what is on staging vs. production, whether it has been tested by agents and/or humans, what looks ready to ship, and what is still iterating.
- Only call out genuinely new blockers or decisions needed (`new_waits` and `stuck` where `new` is true) — never repeat old `stuck` tasks where `new` is false, stale multi-day waiting counts, or boilerplate counters ("In work: ...", "Handled without you: ..."). Each person already gets their own waiting items in their personal brief.
- Never ask a person to toggle internal agent labels, reassign a ticket just to wake an agent, or edit agent allowlist config unless the task is specifically about that config.
- Do not ask a person to check a fact that your tools can read. Read it, and give the result.
- Keep links light and natural — do not hyperlink every clause or dump raw commit hashes. No tables.
- Leave out a fact that you cannot verify, and do not end with a list of things you could not check.
- If nothing meaningful moved and nothing new needs attention, say so in one short line.
- Every claim comes from a tool result of this run.
