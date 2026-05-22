from __future__ import annotations

from typing import Any

VALID_EMBEDDING_BACKENDS = {"api", "cpu", "gpu"}


def embedding_credentials_message(provider: str) -> str:
    """Return the operator-facing missing-credentials message for a provider."""
    provider = (provider or "").lower()
    if provider in {"gemini", "google"}:
        return "Gemini embedding credentials are not configured. Add them in System/Access."
    if provider == "openai":
        return "OpenAI embedding credentials are not configured. Add them in System/Access."
    return "Embedding credentials are not configured. Add them in System/Access."


def embedding_status_detail(info: dict[str, Any]) -> str | None:
    provider = str(info.get("provider") or "").lower()
    status = info.get("status")
    if status == "missing_key":
        return embedding_credentials_message(provider)
    if status == "invalid_config":
        return f"Unknown embedding backend {info.get('backend')!r}. Use gpu, cpu, or api."
    if status == "unavailable":
        return "The local GPU embedding worker is not responding."
    if status == "initializing":
        return "The local GPU embedding worker is still initializing."
    return None


def embedding_status_remediation(info: dict[str, Any]) -> str | None:
    status = info.get("status")
    if status == "missing_key":
        return "Add embedding credentials in System/Access, or choose Local CPU."
    if status == "invalid_config":
        return "Set the memory embedder to Local GPU, Local CPU, Gemini, or OpenAI."
    if status == "unavailable":
        return "Start or inspect the GPU embedding worker, or switch memory to Local CPU/API."
    if status == "initializing":
        return "Wait for the GPU embedding worker to finish loading before running semantic work."
    return None


def finalize_embedding_info(info: dict[str, Any]) -> dict[str, Any]:
    detail = embedding_status_detail(info)
    remediation = embedding_status_remediation(info)
    if detail:
        info["detail"] = detail
    if remediation:
        info["remediation"] = remediation
    info["ready"] = str(info.get("status") or "") == "ready"
    return info


def embedding_backend_label(info: dict[str, Any]) -> str:
    backend = str(info.get("backend") or "").lower()
    if backend == "api":
        return embedding_provider_label(info)
    if backend == "cpu":
        return "CPU"
    if backend == "gpu":
        return "GPU"
    return backend or "unknown"


def embedding_provider_label(info: dict[str, Any]) -> str:
    provider = str(info.get("provider") or "").lower()
    if provider in {"gemini", "google"}:
        return "Gemini"
    if provider == "openai":
        return "OpenAI"
    return provider.upper() if provider else "API"
