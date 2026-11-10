"""Keep the base voice separate from run authority and workspace rules."""

from pathlib import Path

BASE_PROMPT = Path(__file__).with_name("base_prompt.md").read_text().strip()


def build(workspace, principal: str, profile: str, channel_id: str | None = None) -> str:
    """Include matching rules and the skill list. Tools enforce the authority."""
    parts = [BASE_PROMPT, f"Principal: {principal}\nGrant profile: {profile}"]
    parts.extend(
        f"Rule: {r.name}\n{r.body}"
        for r in workspace.matching_rules(principal, profile, channel_id)
    )
    if workspace.skills:
        parts.append(
            "Skills:\n" + "\n".join(f"- {s.name}: {s.description}" for s in workspace.skills)
        )
    return "\n\n".join(parts)
