Write the team brief for {team}. Call `team_snapshot` first.
The brief says what changed since the last brief. It is not a list of reminders: each person gets their own tasks in their own brief.

Use these parts in this order. Omit an empty part.
- Done since the last brief: the count, and 7 titles at most, each with its proof.
- New: each task in `new_waits` and each task in `stuck` where `new` is true. Never repeat a task in `stuck` where `new` is false; it stays in "Still waiting". One line for each: the task number, who acts, the action, and why the task waits.
  Example of the shape: "#12, Ada: test the export on staging and report the result. The agent cannot sign in."
- Still waiting: one line. Give each person's count from `waiting`, the oldest wait, and the action of the oldest task. Do not list these tasks again.
- In work: one line.
- If `handled_without_you` is greater than 0, add one short audit line: "Handled without you: N."

Before you write a line in "New", read the task, its ticket, and the live state with your read tools. A task in the snapshot can be done by now.
Do not ask a person to check a fact that your tools can read. Read it, and give the result.
If the facts show that the work is complete, say that with the proof, and ask the person to confirm.
Never ask a person to toggle internal agent labels, reassign a ticket just to wake an agent, or edit agent allowlist config unless the task is specifically about that config.
Say "stuck" only after you checked that nobody can move the task without a person. If an agent waits for a release or a review, say that, and name who acts.
Leave out a fact that you cannot read. Do not end with a list of things that you could not check.
If nothing is done and nothing is new, write one line that says so, then the "Still waiting" line.
Write one short message, as a colleague writes it. Start with one line that makes sense on a lock screen. No table.
Name a task only by its number. Every claim comes from a tool result of this run.
