"""Shared compact rows. Privacy metadata stays inside the tool server."""

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi.encoders import jsonable_encoder

from illo.sources.search import first_link


@dataclass(frozen=True)
class Compact:
    text: str
    private: bool = False


def short(value, limit):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    prefix = text[:limit - 1]
    return prefix.rsplit(" ", 1)[0] + "…" if " " in prefix else "…"


def line(*values):
    return " · ".join(str(v) for v in values if v is not None and v != "")


def relative(value, *, now=None):
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return short(value, 10)
    seconds = ((now or datetime.now(UTC)) - value).total_seconds()
    age = abs(seconds)
    if age < 60:
        return "now"
    amount, unit = (int(age/60), "m") if age < 3600 else (
        (int(age/3600), "h") if age < 86400 else (int(age/86400), "d")
    )
    return f"{amount}{unit} ago" if seconds >= 0 else f"in {amount}{unit}"


def iso_date(value):
    return value.strftime("%Y-%m-%d") if isinstance(value, datetime) else short(value, 10)


def header(shown, total, hint="Narrow the query, or pass limit=."):
    return f"Showing {shown} of {total}. {hint}"


def rows(values, render, *, total=None, hint="Narrow the query, or pass limit=."):
    return Compact("\n".join([header(len(values), len(values) if total is None else total, hint),
                              *(render(row) for row in values)]),
                   any(row.get("visibility") == "private" or row.get("_private") for row in values))


def lane(row, *, reason=True):
    link = first_link(row.get("links")) or f"lane:{row['id']}"
    repo = row.get("repo")
    your_move = row.get("source") == "t3" and row["state"] == "review"
    label = "your_move" if your_move else row["state"]
    text = line(label + (" stale" if row.get("stale") else ""),
                short(row["title"], 90), row.get("owner"), row["source"],
                short(repo, 100) if repo and repo.lower() not in link.lower() else None,
                relative(row.get("last_signal_at")),
                link, f"id {row['id']}")
    if row.get("is_step"):
        text += " · agent step"
    if your_move:
        text += " · the agent stopped and waits for you"
    if reason and row["state"] in {"waiting_person", "blocked", "failed", "review"}:
        why = short(row.get("summary") if your_move else row.get("last_event") or row.get("summary"), 200)
        if why:
            text += "\n  " + why
    return text


def ask(row):
    return line(row["kind"], short(row["question"], 160),
                f"owner {row['owner']}" if row.get("owner") else None,
                f"due {relative(row['due_at'])}" if row.get("due_at") else None,
                f"lane {short(row['lane_title'], 60)}" if row.get("lane_title") else None,
                first_link(row.get("lane_links")), f"id {row['id']}")


def hit(row):
    # A ref remains available even when the source also has a link.
    return line(row.kind, short(row.title, 100), iso_date(row.at), short(row.snippet, 220),
                row.link, row.source_ref)


def github(row, repo=None):
    labels = [x.get("name") if isinstance(x, dict) else x for x in row.get("labels", [])]
    owners = [x.get("login") if isinstance(x, dict) else x for x in row.get("assignees", [])]
    return line(f"{repo or row.get('repo', '')}#{row['number']}", row.get("state"),
                short(row.get("title"), 100), short(", ".join(filter(None, labels)), 160),
                short(", ".join(filter(None, owners)), 100),
                f"updated {relative(row['updated_at'])}" if row.get("updated_at") else None,
                row.get("html_url"))


DATA_SOURCE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,80}$")
# "<", the full-width "＜", and the small "﹤", then an optional "/", then "outside data"
# with any space, underscore, or hyphen, in any case.
DATA_MARKER = re.compile(r"[<＜﹤]\s*/?\s*outside[\s_\-]*data", re.IGNORECASE)


def _visible(text: str) -> str:
    """Show format controls (zero-width, bidi) as text, so they cannot hide a marker."""
    return "".join(
        f"[U+{ord(c):04X}]" if unicodedata.category(c) == "Cf" else c for c in text
    )


def data_text(source: str, text) -> str:
    """Mark data for the model. Only this function writes the outside-data markers."""
    if not DATA_SOURCE.fullmatch(source):
        raise ValueError("The data source name is invalid")
    text = str(text or "")
    probe = unicodedata.normalize(
        "NFKC", "".join(c for c in text if unicodedata.category(c) != "Cf")
    )
    if DATA_MARKER.search(probe):
        # Hostile or nested text only: make every look-alike visible, then break each marker.
        text = unicodedata.normalize("NFKC", _visible(text))
        text = DATA_MARKER.sub(lambda m: "‹" + m.group(0)[1:], text)
    return f'<outside_data source="{source}">\n{text}\n</outside_data>'


def outside_result(value):
    """Convert a connector result to the format that the model reads."""
    text = value["data"] if isinstance(value["data"], str) else json_text(value["data"])
    return data_text(value["source"], text)


def body(value, limit=3000):
    value = str(value or "")
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n[{len(value)-limit} characters omitted]"


def github_detail(row, repo):
    if not row:
        return None
    comments = row.get("comments_data") or []
    # Keep the state and evidence needed to assess a pull request.
    facts = {key: row[key] for key in ("merged", "mergeable", "mergeable_state", "draft")
             if row.get(key) is not None}
    if row.get("head"):
        facts["head"] = row["head"].get("sha")
    checks = row.get("checks") or []
    reviews = row.get("reviews") or []
    text = [github(row, repo), body(row.get("body")),
            f"Comments: showing {min(5, len(comments))} of {max(len(comments), row.get('comments', 0))}."]
    for comment in comments[-5:]:
        text.append(line(comment.get("user", {}).get("login"), iso_date(comment.get("created_at")),
                         comment.get("html_url"), body(comment.get("body"), 500)))
    if facts:
        text.append(json.dumps(facts, ensure_ascii=False, separators=(",", ":")))
    for check in checks:
        text.append(line("check", check.get("name"), check.get("status"), check.get("conclusion"),
                         check.get("html_url")))
    status = row.get("status") or {}
    if status.get("state"):
        text.append(line("status", status["state"]))
    for review in reviews:
        text.append(line("review", review.get("user", {}).get("login"), review.get("state"),
                         iso_date(review.get("submitted_at"))))
    return data_text("github", "\n".join(text))


def json_text(value):
    return json.dumps(jsonable_encoder(value), ensure_ascii=False, separators=(",", ":"))


def external_list(value, *, limit=15, offset=0):
    """Keep the outside-data marker while reducing Google list rows."""
    data = value.get("data", {})
    key = next((key for key in ("messages", "files", "items") if key in data), None)
    if key is None:
        return value
    values = data[key]
    def render(row):
        start = row.get("start") or {}
        return line(short(row.get("summary") or row.get("name") or row.get("subject"), 160),
                    start.get("dateTime") or start.get("date"),
                    short(row.get("description") or row.get("snippet"), 220),
                    row.get("htmlLink") or row.get("webViewLink"), row.get("id"))
    text = rows(values[offset:offset+limit], render, total=len(values),
                hint="Pass offset= / limit= for more, or read one id.").text
    if data.get("nextPageToken"):
        text += "\nThe source has more results. Narrow the query or the date range."
    return {**value, "data": text}
