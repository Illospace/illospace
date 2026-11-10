"""Build small Block Kit cards from ledger data. No model supplies card state."""

import hashlib
import html
import json
import unicodedata
from datetime import UTC, datetime
from urllib.parse import urlsplit

EMOJI = {"decision": "🔷", "action": "🔧", "review": "👀", "reminder": "⏰"}


def escape(value):
    return html.escape(str(value), quote=False)


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000] or " "}}


def button(label: str, action: str, primary: bool = False) -> dict:
    value = {
        "type": "button",
        "text": {"type": "plain_text", "text": label[:30]},
        "action_id": action,
    }
    if primary:
        value["style"] = "primary"
    return value


def digest(blocks: list[dict]) -> str:
    return hashlib.sha256(json.dumps(blocks, sort_keys=True).encode()).hexdigest()


def relative(time: datetime, now: datetime) -> str:
    minutes = int((time - now).total_seconds() / 60)
    if abs(minutes) < 1:
        return "now"
    amount = f"{abs(minutes)} min" if abs(minutes) < 60 else f"{abs(minutes) // 60} hr"
    return f"in {amount}" if minutes > 0 else f"{amount} ago"


def links_text(links: list) -> str:
    result = []
    for link in links[:3]:
        url = link.get("url", "") if isinstance(link, dict) else str(link)
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme in {"https", "http"} and parsed.hostname:
            result.append(f"<{escape(url).replace('|', '%7C')}|{escape(parsed.hostname)}>")
    return " · ".join(result)


def ask_card(ask: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    lane = ask.get("lane") or {}
    blocks = [
        section(f"{EMOJI[ask['kind']]} {escape(lane.get('title') or 'Ask')}"),
        section(f"*{escape(ask['question'])}*"),
    ]
    proof = ask.get("proof") or {}
    text = proof.get("text") or proof.get("body") or ""
    if text:
        blocks.append(section(f"Why: {escape(str(text).splitlines()[0])}"))
    links = links_text(proof.get("links", []))
    if links:
        blocks.append(section(links))
    if ask["state"] == "open":
        details = [
            f"• {escape(o['label'])}: {escape(o.get('detail', ''))}"
            for o in ask["options"]
            if o.get("detail") or len(o["label"]) > 30
        ]
        if details:
            blocks.append(section("\n".join(details)))
        options = sorted(ask["options"], key=lambda o: o["id"] != ask["recommended_option"])
        for offset in range(0, len(options), 5):
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        button(
                            o["label"],
                            f"ask:{ask['id']}:opt:{o['id']}",
                            o["id"] == ask["recommended_option"],
                        )
                        for o in options[offset : offset + 5]
                    ],
                }
            )
        elements = []
        recommended = next((o for o in options if o["id"] == ask["recommended_option"]), None)
        if recommended and ask.get("allow_rule", True):
            elements.append(
                button(
                    f"Always: {recommended['label']}", f"ask:{ask['id']}:always:{recommended['id']}"
                )
            )
        elements.append(button("Other…", f"ask:{ask['id']}:other"))
        blocks.append({"type": "actions", "elements": elements})
    elif ask["state"] == "answered":
        rule = ask.get("rule")
        option = next(
            (o["label"] for o in ask["options"] if o["id"] == ask["answer_option"]),
            ask["answer_text"] or "",
        )
        text = (
            f'Answered by the rule "{escape(rule["text"])}"'
            if rule
            else f"Answered: {escape(option)} — {escape((ask.get('owner') or {}).get('name', 'Unknown'))}, {ask['answered_at'].isoformat(timespec='minutes')}"
        )
        if ask["delivery_state"] == "acked":
            text += " · the agent has it"
        blocks.append(section(text))
    else:
        blocks.append(section("This question is closed. No answer is needed."))
    context = []
    requester = ask.get("requester") or {}
    if requester.get("label"):
        context.append(
            f"From {escape(requester['label'])}"
            + (f" of {escape(requester['person_name'])}" if requester.get("person_name") else "")
            + (f" on {escape(lane['machine'])}" if lane.get("machine") else "")
        )
    if ask.get("due_at"):
        context.append(f"due {relative(ask['due_at'], now)}")
    if context:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(context)}]}
        )
    return blocks


def draft_preview(draft: dict) -> str | None:
    """Keep the entire proposed action visible before a person can send it."""
    content = draft["content"]
    if draft.get("kind") == "asks_answer":
        content = draft.get("answer_preview")
        if content is None:
            return None
    text = (
        next(iter(content.values()))
        if len(content) == 1 and set(content) <= {"text", "body"}
        else json.dumps(content, ensure_ascii=False, default=str)
    )
    visible = "".join(
        (f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
        if unicodedata.category(c) == "Cf"
        else c
        for c in str(text)
    )
    escaped = escape(visible)
    return escaped if len(escaped.replace("\n", "\n> ")) <= 120000 else None


def draft_card(draft: dict) -> list[dict]:
    text = draft_preview(draft)
    blocks = [section(f"Draft for {escape(draft['destination'])}")]
    if text is None:
        blocks.append(section("This draft is too long to review in Slack. It stays unsent."))
        return blocks
    quoted = text.replace("\n", "\n> ")
    if len(quoted) > 120000:
        blocks.append(section("This draft is too long to review in Slack. It stays unsent."))
        return blocks
    for offset in range(0, max(1, len(quoted)), 2800):
        blocks.append(section("> " + quoted[offset : offset + 2800]))
    if draft["state"] == "pending":
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    button("Send", f"draft:{draft['id']}:approve", True),
                    button("Do not send", f"draft:{draft['id']}:reject"),
                ],
            }
        )
    elif draft["state"] == "sent":
        blocks.append(section(f"Sent — {draft['decided_at'].isoformat(timespec='minutes')}"))
    elif draft["state"] == "approved":
        blocks.append(
            section(
                "Delivery could not be confirmed. Check the destination."
                if draft.get("send_error")
                else "Send approved. Delivery waits for confirmation."
            )
        )
    else:
        blocks.append(section("Not sent"))
    return blocks
