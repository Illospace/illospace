"""Check a wait before a reminder. Code binds each result to one waiting period."""

from psycopg.types.json import Jsonb

from illo.db import all_rows, one
from illo.deep import start
from illo.gateway.principal import Authority
from illo.ledger import tasks
from illo.tools.registry import Refusal

# The page tools exist only when task pages are on. The prompt names them only then.
PAGE = ("When the task waits for a person to review it, use `page_read`. If the page has no blocks "
        "or `stale` is true, write the page with `page_write`.\n\n")
# The look at return time. It fills the page, and it sends no reminder.
AT_RETURN = -1
JOB = """Task {id} waits for {person} since {hours} hours. The record says: {needed}
Before Illo reminds a person, find out if a person is needed. A wait ends in one of three ways. Find which one is true.

Read the task first: `task_get` with id {id}. Read its brief, result, links, and events.
Then read the live state where the work is. The task text can be old: do not trust it for the current state.
• Code and config: the repository in the sandbox, at the branch that production uses.
• Release state: `github_get_commit` gives the checks of a commit. A release job that passed on the production branch shows that the commit is live.
• Tickets and pull requests: the GitHub tools.
• A public page or an outside service: the web tools.
• Logs and business data: the connectors, if they are connected.

{page}Then call `wait_result` one time:
• `complete`: the wanted result of the task is live where the problem was. A merge alone is not complete. Give the proof: each fact with its web address.
• `restart`: an agent holds the task, its start failed or it stopped, and the cause is gone now.
• `person`: a person must act. Write the one action. The person must be able to do it from your message alone. Say why an agent cannot do it. Give the facts that you verified, so the person does not check them again.

If you cannot verify a fact that `complete` needs, choose `person` and name that fact.
Write the message for a busy person: 90 words at most, the result first. Use plain web addresses, no markup.
Your final answer is a short log: what you read, and what you decided.
"""


async def current(conn, row):
    """Find the first event of this wait by record order. Later messages do not reset it."""
    target = row["waits_for"]
    events = await all_rows(conn, "SELECT * FROM task_events WHERE task_id=%s ORDER BY id DESC", (row["id"],))
    since, since_id, first = row["created_at"], None, None
    needed = ("read the returned work on its card, then press Done or Send back"
              if row["state"] == "review" else "you hold it")
    reminded = False
    for event in events:
        if event["data"].get("waits_for") != target:
            break
        since, since_id, first = event["at"], event["id"], event
        if event["kind"] == "question":
            needed = event["text"]
        reminded = reminded or (event["kind"] == "reminded" and event["data"].get("to") == target)
    if first and ((first["kind"] == "wake_failed" and first["data"].get("final"))
                  or first["data"].get("escalated")):
        needed = " ".join(filter(None, (first["text"], first["data"].get("guidance"))))
    return {"since": since, "since_event_id": since_id, "needed": needed, "reminded": reminded}


def key(wait):
    return f"wait:{wait['task_id']}:{wait['since_event_id'] or 0}:{wait['round']}"


def request(row, period, person, now, round, *, pages=False):
    wait = {"task_id": row["id"], "target": row["waits_for"],
            "since_event_id": period["since_event_id"], "round": round}
    return {"wait": wait, "job": JOB.format(id=row["id"], person=person["name"],
        hours=f"{max(0, (now-period['since']).total_seconds()/3600):.0f}", needed=period["needed"],
        page=PAGE if pages else "")}


async def queue(ctx, conn, pending):
    # This connection has no task lock. Admission uses the deep lock first.
    await conn.execute("SELECT pg_advisory_xact_lock(15001500)")
    if await one(conn, "SELECT id FROM deep_runs WHERE dedupe_key=%s", (key(pending["wait"]),)):
        return None
    active = await one(conn, "SELECT count(*) AS n FROM deep_runs WHERE kind='check' AND state IN ('queued','running')")
    if active["n"] >= 2:
        return None
    deep_id = await start(ctx, conn, kind="check", job=pending["job"],
        authority=Authority("team", "routine"), deliver={"none": True},
        dedupe_key=key(pending["wait"]))
    if deep_id:
        await conn.execute("UPDATE deep_runs SET context=context || %s WHERE id=%s",
                           (Jsonb({"wait": pending["wait"]}), deep_id))
    return deep_id


@tasks._mutation
async def apply(conn, *, ctx, run, outcome, message):
    deep = await one(conn, "SELECT * FROM deep_runs WHERE id=%s FOR UPDATE", (run.get("deep_run_id"),))
    if not deep or deep["kind"] != "check" or deep["run_id"] != run["id"]:
        raise Refusal("This run has no wait check.")
    wait = deep["context"]["wait"]
    if wait.get("applied"):
        raise Refusal("This wait check already has a result.")
    row = await one(conn, "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (wait["task_id"],))
    period = await current(conn, row) if row else None
    if (not row or row["state"] in {"done", "cancelled"} or row["visibility"] != "team"
            or row["waits_for"] != wait["target"] or period["since_event_id"] != wait["since_event_id"]):
        outcome, text = "stale", "The task changed. Illo made no change."
    else:
        entry = next((e for e in ctx.workspace.lists if e.key == row["list_key"]), None)
        if outcome == "complete":
            if entry and entry.done == "person" and row["state"] in {"todo", "working"}:
                row = await tasks._state(conn, ctx.workspace, row, "review", "code", "returned",
                    "Illo checked the live state. The work looks complete.",
                    {"source": "wait_check", "detail": message})
                await tasks._settle(conn, ctx.workspace, row, ctx)
            else:
                outcome = "person"
        if outcome == "restart":
            previous = await one(conn, """SELECT id FROM task_events WHERE task_id=%s
                AND kind='released' AND data->>'source'='wait_check' AND at>=%s LIMIT 1""",
                (row["id"], row["holder_since"]))
            if ((row["holder"] or "").startswith("agent:") and row["state"] in {"todo", "working"}
                    and row["waits_for"].startswith("person:") and not previous):
                await conn.execute("UPDATE tasks SET waits_for=holder,stopped_at=NULL WHERE id=%s", (row["id"],))
                await tasks._event(conn, row, "code", "released", message, {"source": "wait_check"})
            else:
                outcome = "person"
        if outcome == "person" and wait["round"] == 0 and not period["reminded"]:
            await tasks._event(conn, row, "code", "reminded", message,
                               {"to": wait["target"], "source": "wait_check"})
        elif outcome == "person":
            # This look sends nothing. The record keeps what Illo found, for the briefs and for answers.
            await tasks._event(conn, row, "code", "message", message, {"source": "wait_check"})
        text = "Illo recorded the wait check: " + outcome + "."
    await conn.execute("UPDATE deep_runs SET context=jsonb_set(context,'{wait,applied}',%s) WHERE id=%s",
                       (Jsonb(outcome), deep["id"]))
    return text
