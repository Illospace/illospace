"""Agent kernel boundary for normalized runtime envelopes."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from brain.kernel.runtime.envelope import RunEnvelope, RunResult

logger = logging.getLogger("brain.kernel.runtime.kernel")


def _direct_agent_entrypoints():
    from brain.systems.runs.direct_agent import run_agent, run_agent_async

    return run_agent, run_agent_async


@dataclass
class AgentKernel:
    """Small kernel facade around the current agent loop.

    This is intentionally a boundary first, not a rewrite. Later PRs can move
    context runtime, provider transport, tool execution, and telemetry behind
    this class without changing callers that already pass a ``RunEnvelope``.
    """

    def run(self, envelope: RunEnvelope, **run_agent_overrides) -> RunResult:
        logger.info(
            "agent_kernel_run origin=%s run_id=%s trace_id=%s session_id=%s",
            envelope.origin,
            envelope.run_id,
            envelope.trace_id,
            envelope.session_id,
        )
        kwargs = envelope.to_run_agent_kwargs()
        kwargs.update(run_agent_overrides)
        run_agent, _ = _direct_agent_entrypoints()
        result = run_agent(**kwargs)
        return RunResult.from_agent_result(envelope, result)

    def invoke_agent_result(self, envelope: RunEnvelope, **run_agent_overrides):
        """Run the kernel and return the AgentResult."""
        return self.run(envelope, **run_agent_overrides).agent_result

    async def run_async(self, envelope: RunEnvelope, **run_agent_overrides) -> RunResult:
        logger.info(
            "agent_kernel_run_async origin=%s run_id=%s trace_id=%s session_id=%s",
            envelope.origin,
            envelope.run_id,
            envelope.trace_id,
            envelope.session_id,
        )
        kwargs = envelope.to_run_agent_kwargs()
        kwargs.update(run_agent_overrides)
        _, run_agent_async = _direct_agent_entrypoints()
        result = await run_agent_async(**kwargs)
        return RunResult.from_agent_result(envelope, result)

    async def invoke_agent_result_async(self, envelope: RunEnvelope, **run_agent_overrides):
        """Run the async kernel and return the AgentResult."""
        return (await self.run_async(envelope, **run_agent_overrides)).agent_result


_DEFAULT_KERNEL = AgentKernel()


def run_envelope(envelope: RunEnvelope, **run_agent_overrides) -> RunResult:
    """Execute a run envelope and return a structured kernel result."""
    return _DEFAULT_KERNEL.run(envelope, **run_agent_overrides)


def invoke_run_envelope(envelope: RunEnvelope, **run_agent_overrides):
    """Execute a run envelope and return the AgentResult."""
    return _DEFAULT_KERNEL.invoke_agent_result(envelope, **run_agent_overrides)


async def run_envelope_async(envelope: RunEnvelope, **run_agent_overrides) -> RunResult:
    """Execute a run envelope from async runtime code."""
    return await _DEFAULT_KERNEL.run_async(envelope, **run_agent_overrides)


async def invoke_run_envelope_async(envelope: RunEnvelope, **run_agent_overrides):
    """Execute a run envelope from async runtime code and return the AgentResult."""
    return await _DEFAULT_KERNEL.invoke_agent_result_async(envelope, **run_agent_overrides)
