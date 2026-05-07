#!/usr/bin/env python3
"""Delegation wrapper: inject quality gates into child agent prompts.

Wraps a child agent prompt with the verbatim user request as acceptance
criteria, ensuring the delegation stays aligned with the original ask.

Closes #70 (Structural Learning — Auto-Inject Quality Gate).
"""

import os
import sys
import textwrap

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

QUALITY_GATE_TEMPLATE = textwrap.dedent("""\
    ## Acceptance Criteria (from original user request)
    
    The user's verbatim request:
    > {user_ask}
    
    Task assigned to you:
    > {task}
    
    ---
    
    ## Your Instructions
    
    {prompt}
    
    ---
    
    ## Quality Gate — BEFORE completing, verify:
    1. Your output directly addresses the user's verbatim request above
    2. All acceptance criteria from the original ask are met
    3. No scope drift — only do what was asked
    4. If you cannot fully satisfy the request, explain what's missing and why
""")


def wrap_delegation_prompt(user_ask: str, task: str, prompt: str) -> str:
    """Wrap a child agent prompt with quality gate and acceptance criteria.

    Args:
        user_ask: The original verbatim user message/request.
        task: Short description of the delegated task.
        prompt: The child agent prompt to enhance.

    Returns:
        Enhanced prompt with acceptance criteria and quality gate.
    """
    if not user_ask or not user_ask.strip():
        raise ValueError("user_ask cannot be empty")
    if not prompt or not prompt.strip():
        raise ValueError("prompt cannot be empty")

    return QUALITY_GATE_TEMPLATE.format(
        user_ask=user_ask.strip(),
        task=task.strip() if task else "(not specified)",
        prompt=prompt.strip(),
    )
