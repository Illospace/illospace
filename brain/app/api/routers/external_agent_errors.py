"""Shared HTTP error mapping for external-agent bridge routes."""

from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException

from brain.systems.external_agents import service as external_agents


def raise_external_agent_http_error(exc: Exception) -> NoReturn:
    if isinstance(exc, external_agents.ExternalAgentAuthError):
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if isinstance(exc, external_agents.ExternalAgentPermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, external_agents.ExternalAgentNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, external_agents.ExternalAgentError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc
