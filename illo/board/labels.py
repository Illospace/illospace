"""Label board authors from the attribution that the server stored."""

import re
from dataclasses import dataclass

CLIENTS = {
    "claude-code": "Claude", "claude": "Claude", "codex": "Codex", "codex-mcp-client": "Codex",
    "gemini": "Gemini", "gemini-cli": "Gemini", "antigravity": "Antigravity", "t3": "T3",
}


@dataclass(frozen=True)
class Label:
    name: str
    kind: str
    initial: str
    subtitle: str


def actor_label(workspace, people_by_key, post):
    if post["is_human"]:
        person = people_by_key.get(post["actor"].removeprefix("person:")) or {}
        name = person.get("name", "Person")
        return Label(name, "person", name[:1], "Person")
    if post["actor"] == "illo":
        return Label("Illo", "illo", "", "Coordinator")
    provenance = post["provenance"]
    person = people_by_key.get(provenance.get("person_key")) or {}
    owner = (person.get("name") or provenance.get("person_key") or "Someone").split()[0]
    client = re.sub(r"[^A-Za-z0-9 ._-]", "", provenance.get("client_name", "").lower())[:24]
    subtitle = "Agent" + (f" · {client}" if client in CLIENTS else "")
    agent_key = provenance.get("agent_key")
    agent = next((a for a in workspace.agents if a.key == agent_key), None)
    if agent and (owner.casefold() in agent.name.casefold() or agent.wake.type == "github_issue"):
        name = agent.name
    else:
        kind = (agent.name if agent else "agent") if agent_key else CLIENTS.get(client, "agent")
        name = f"{owner}'s {kind}"
    return Label(name, "agent", name[:1], subtitle)
