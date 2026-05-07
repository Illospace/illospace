#!/usr/bin/env python3
"""Prompt builder — assemble prompts from templates + guardrails + context.

Loads a versioned template, injects guardrails from the skill system,
and includes the user's original ask for alignment.

Closes #74 (Self-Improving Prompts).
"""

import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.systems.prompts.templates import get_template, record_use, seed_templates


def _get_guardrails(task: str) -> list[str]:
    """Query the skill plan system for relevant guardrails."""
    try:
        from io import StringIO
        import argparse
        import brain.app.cli.skills as skills_mod

        buf = StringIO()
        args = argparse.Namespace(task=task)

        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            skills_mod.cmd_plan(args)
        finally:
            sys.stdout = old_stdout

        plan = json.loads(buf.getvalue())
        return plan.get("guardrails", [])
    except Exception:
        return []


def build_prompt(
    template_name: str,
    task: str,
    user_ask: str | None = None,
    inject_guardrails: bool = True,
    **kwargs,
) -> str:
    """Build a fully assembled prompt from a template.

    Args:
        template_name: Name of the template (e.g. "bug_fix").
        task: Short task description to fill {task} placeholder.
        user_ask: Original user request (included verbatim if provided).
        inject_guardrails: Whether to query the skill system for guardrails.
        **kwargs: Additional placeholders for the template.

    Returns:
        Fully assembled prompt string.
    """
    # Ensure seed templates exist
    seed_templates()

    tmpl = get_template(template_name)
    if not tmpl:
        raise ValueError(f"Template '{template_name}' not found")

    # Get guardrails
    guardrail_lines = []
    if inject_guardrails:
        guardrails = _get_guardrails(task)
        if guardrails:
            guardrail_lines = [f"- {g}" for g in guardrails]

    guardrail_block = ""
    if guardrail_lines:
        guardrail_block = "### Learned Guardrails (from past experience)\n" + "\n".join(guardrail_lines)

    # Render template
    render_kwargs = {"task": task, "guardrails": guardrail_block, **kwargs}
    text = tmpl["template_text"]
    placeholders = re.findall(r'\{(\w+)\}', text)
    for ph in placeholders:
        if ph not in render_kwargs:
            render_kwargs[ph] = ""
    prompt = text.format(**render_kwargs)

    # Prepend user ask if provided
    if user_ask:
        prompt = (
            f"## Original User Request\n> {user_ask.strip()}\n\n"
            f"---\n\n{prompt}"
        )

    # Record usage
    record_use(template_name)

    return prompt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build prompts from templates")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Build a prompt")
    p_build.add_argument("--template", "-t", required=True)
    p_build.add_argument("--task", required=True)
    p_build.add_argument("--user-ask", "-u")

    args = parser.parse_args()

    if args.command == "build":
        try:
            prompt = build_prompt(
                template_name=args.template,
                task=args.task,
                user_ask=args.user_ask,
                inject_guardrails=True,
            )
            print(prompt)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
