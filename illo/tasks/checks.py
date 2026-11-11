"""Evaluate proof with named checks. A check never changes a task."""

from collections.abc import Awaitable, Callable

from illo.db import all_rows

Check = Callable[[object, object, dict], Awaitable[tuple[bool, str]]]
_checks: dict[str, Check] = {}


def register(name: str, check: Check) -> None:
    if not name or name == "person" or name == "pieces" or name.startswith("link:") or name in _checks:
        raise ValueError("The check name is reserved or already present")
    _checks[name] = check


def known(name: str) -> bool:
    return name in {"person", "pieces"} or (name.startswith("link:") and len(name) > 5) or name in _checks


async def evaluate(ctx, conn, task, rule) -> list[dict]:
    names = ["person"] if rule == "person" else rule
    result = []
    for name in names:
        if name == "person":
            ok, text = False, "A person must confirm the work."
        elif name.startswith("link:"):
            ok = any(link.get("kind") == name[5:] for link in task["links"])
            text = "The proof link is present." if ok else "The proof link is missing."
        elif name == "pieces":
            rows = await all_rows(conn, "SELECT state FROM tasks WHERE parent_id=%s", (task["id"],))
            ok = all(row["state"] == "done" for row in rows)
            text = "All pieces are done." if ok else "Some pieces are not done."
        elif name in _checks:
            try:
                ok, text = await _checks[name](ctx, conn, task)
            except Exception:
                ok, text = False, "The check could not run. Try it again after you fix the check."
        else:
            ok, text = False, "This check is not registered."
        result.append({"name": name, "ok": ok, "text": text})
    return result
