"""Replace a broken person-facing answer with facts from this run's audit."""

import logging
import re

from illo.db import all_rows
from illo.ledger import sensors

log = logging.getLogger(__name__)


def broken(text):
    if re.search(r'<(?:#\||\||\s+>)', text):
        return True
    # A link label is readable text. A bare link or mention is not.
    plain = re.sub(r'<[^>|]*\|([^>]*)>', r'\1', text)
    plain = re.sub(r'<[^>]*>', '', plain)
    return sum(character.isalnum() for character in plain) < 3


async def guard(conn, run_id, text):
    if not broken(text):
        return text
    rows = await all_rows(conn, """SELECT tool,args,decision,target FROM audit WHERE run_id=%s
        AND (decision='draft' OR (decision='allow' AND tool = ANY(%s))) ORDER BY id""",
        (run_id, ["task_create", "task_update", "task_hand", "task_post", "task_done", "task_reopen", "task_cancel"]))
    lines = ["Illo could not write a clear answer."]
    for row in rows:
        args = row["args"]
        target = args.get("id") or args.get("task_id")
        # Use ids only. Free text in audit arguments can contain outside instructions.
        label = row.get("target") or (f"task {target}" if isinstance(target, int) else "the task list")
        if row["decision"] == "draft":
            lines.append("A draft waits for your approval: " + row["tool"].replace('_', ' ') + ": " + label + ".")
        else:
            lines.append(row["tool"].replace('_', ' ').capitalize() + ": " + label + ".")
    log.warning("The final answer guard replaced text for run %s", run_id)
    await sensors.error(conn, "brain:final-text", f"The final answer guard replaced text for run {run_id}.")
    return "\n".join(lines)
