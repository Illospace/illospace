"""Agent kernel boundary for normalized runtime envelopes."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from brain.kernel.runtime.envelope import RunEnvelope, RunResult

logger = logging.getLogger("brain.kernel.runtime.kernel")


@dataclass
class AgentKernel:
    """Small kernel facade around the current agent loop.

    This is intentionally a boundary first, not a rewrite. Later PRs can move
    context runtime, provider transport, tool execution, and telemetry behind
    this class without changing callers that already pass a ``RunEnvelope``.
    """

    def run(self, envelope: RunEnvelope) -> RunResult:
        from brain.systems.runs.direct_agent import run_agent

        logger.info(
            "agent_kernel_run origin=%s run_id=%s trace_id=%s session_id=%s",
            envelope.origin,
            envelope.run_id,
            envelope.trace_id,
            envelope.session_id,
        )
        result = run_agent(**envelope.to_run_agent_kwargs())
        return RunResult.from_agent_result(envelope, result)

    def invoke_agent_result(self, envelope: RunEnvelope):
        """Run the kernel and return the AgentResult."""
        return self.run(envelope).agent_result


_DEFAULT_KERNEL = AgentKernel()


def run_envelope(envelope: RunEnvelope) -> RunResult:
    """Execute a run envelope and return a structured kernel result."""
    return _DEFAULT_KERNEL.run(envelope)


def invoke_run_envelope(envelope: RunEnvelope):
    """Execute a run envelope and return the AgentResult."""
    return _DEFAULT_KERNEL.invoke_agent_result(envelope)
