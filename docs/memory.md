# Team memory

Illo keeps three kinds of memory. Each kind has one home.

| Kind | Home |
| --- | --- |
| How the team does one type of work | `lists/<key>.md` |
| Facts about the team or product | `notes/*.md` |
| What happened | The task record and the old archive |

Start with the general list. Teach Illo other types of work through chat.
You do not need to edit a file.

1. Ask Illo for work in a DM or channel.
2. Illo reads the matching list. It searches memory before it asks for missing facts.
3. If the holder, result place, or Done rule is missing, Illo asks one question and makes no task yet.
4. Reply in the same thread. Illo saves the lasting answer and makes the task.
5. Illo says `I will remember: …` after the save succeeds.

Illo also saves lasting facts that you state without a request to remember.
It asks you to confirm an answer from the old archive, with its date.
A one-time task can use the general list without a question.

After the answer, code posts `Saved to memory: <name>.` with **Correct** and **Forget**.
**Correct** asks you to reply in the same thread with the correct text.
Your next reply replaces the note text or the list instructions.
**Forget** deletes a note or restores a list's earlier text.
For a new list, Forget deletes the list only if no open task uses it.
An old notice cannot replace a later file edit.
Only the saver or an active owner can use these buttons. They expire after 7 days.
After that, tell Illo the change in words.

Any active person can save a team note or change a list's description and instructions.
Only an owner or the list's owner can create a list or change its name, holders, or Done rule.
This also applies to its default holder, reviewer, and Slack channel.
Set `memory.list_changes: everyone` in `illo.yaml` to let all active people make these changes.
The default is `owners`. A private list stays private to its owner.

Only an untainted DM or team channel run can save directly.
Agents can propose notes with the existing Keep card. They cannot save team memory directly.
After a personal read, a note waits on a Keep card. A list change is refused.
A private channel for team members still saves to team memory.
Only a known person's words become memory. Outside text never does.
Never save secrets, personal data, or one-time details.
Code checks the person and the run. The model must check the meaning of the text.

Each person can make at most 20 saves per hour. Pending note proposals count too.
A correction counts as a save. A title has at most 200 characters.
Note text and list instructions have at most 20000 characters.
Note `about` and list descriptions have at most 2000 characters.

You can edit the Markdown files by hand. Keep their YAML front matter.
The workspace loader checks them on reload. Notes enter the search index.
Chat changes pass the full workspace check before they replace the live file.
The `memory_changes` table keeps the earlier text for the buttons and the audit records each action.
Each direct save, correction, and Forget makes a local git commit when the workspace is a git repository.
A commit failure leaves the file saved and records a sensor error.
See [Workspace files](workspace.md) and [Install](install.md) for the file format and host push command.
