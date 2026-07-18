#!/usr/bin/env python3
"""
Shared helper for calling the agent loop from Python.

Single source of truth for all automated agent invocations.
Wraps core.agent.run_agent with a CLI-friendly interface.
"""

import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.systems.runs.failures import failure_category_for_error, public_run_failure


logger = logging.getLogger(__name__)


def _public_failure_message(error: BaseException | str | None) -> str:
    category = failure_category_for_error(error)
    failure = public_run_failure("failed", category)
    return str((failure or {}).get("message") or "")


def call_agent(
    session_id: str,
    message: str,
    thinking: str = "high",
    output_file: str | None = None,
    model: str | None = None,
) -> dict:
    """
    Call the agent loop and return the result.

    Args:
        session_id: Unique session identifier for this agent call
        message: The message/prompt to send
        thinking: Thinking level (none|low|medium|high)
        output_file: If set, check this file for output (agent may write directly)
        model: Optional model override (e.g. "openai/gpt-5.4")

    Returns:
        {
            "success": bool,
            "text": str,         # Agent's text response (if any)
            "from_file": bool,   # Whether output came from output_file
            "error": str | None,
        }
    """
    from brain.systems.runs.direct_agent import BRAIN_TOOLS
    from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent

    try:
        spec = build_direct_agent_invocation(
            message=message,
            session_id=session_id,
            model=model,
            thinking=thinking,
            tools=BRAIN_TOOLS,
            persist_session=False,
            tool_call_source="agent_cli",
        )
        result = invoke_direct_agent(spec)

        # Check if agent wrote output to a file directly
        if result.success and output_file and os.path.exists(output_file):
            with open(output_file) as f:
                text = f.read()
            if text.strip():
                return {"success": True, "text": text, "from_file": True, "error": None}

        if result.success and result.output.strip():
            return {"success": True, "text": result.output, "from_file": False, "error": None}
        elif result.success:
            return {"success": False, "text": "", "from_file": False,
                    "error": "Agent returned empty response"}
        else:
            error = result.error or "Agent failed"
            logger.warning("agent_cli_failed error=%s", error)
            return {"success": False, "text": "", "from_file": False,
                    "error": _public_failure_message(error)}

    except Exception as e:
        logger.exception("agent_cli_exception")
        return {"success": False, "text": "", "from_file": False,
                "error": _public_failure_message(e)}


def extract_json(text: str) -> dict | None:
    """Extract a JSON object from text that may contain surrounding content."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in code fence
    fence_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find outermost JSON object
    json_start = text.find('{')
    json_end = text.rfind('}') + 1
    if json_start >= 0 and json_end > json_start:
        try:
            return json.loads(text[json_start:json_end])
        except json.JSONDecodeError:
            pass

    return None
