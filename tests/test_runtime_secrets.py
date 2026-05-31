from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_ENV_RE = re.compile(r"(API[_-]?KEY|AUTH|CREDENTIAL|PASSWORD|SECRET|TOKEN)", re.IGNORECASE)


@pytest.mark.asyncio
async def test_agent_tool_runtime_secret_uses_central_agent_access(monkeypatch):
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, read_runtime_secret

    calls = []

    async def read_agent_secret_for_runtime(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key_name"], "value": "vault-value"}

    async def read_agent_secret_for_runtime_with_key(key_name, **kwargs):
        kwargs["key_name"] = key_name
        return await read_agent_secret_for_runtime(**kwargs)

    monkeypatch.setattr(
        "brain.systems.vault.agent_access.read_agent_secret_for_runtime",
        read_agent_secret_for_runtime_with_key,
    )

    value = await read_runtime_secret(
        "github_token",
        context=RuntimeSecretContext(
            actor_user_id="user-1",
            org_id="org-1",
            run_id=42,
            idea_id="thread-1",
        ),
        reason="Fetch repository data for this run.",
        requested_by="test_tool",
        access="agent_tool",
    )

    assert value == "vault-value"
    assert calls == [
        {
            "key_name": "GITHUB_TOKEN",
            "reason": "Fetch repository data for this run.",
            "user_id": "user-1",
            "org_id": "org-1",
            "run_id": 42,
            "idea_id": "thread-1",
            "requested_by": "test_tool",
            "project_slug": None,
            "project_slugs": None,
            "target_registry_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_service_runtime_secret_prefers_vault_before_env(monkeypatch):
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, read_runtime_secret

    calls = []

    async def get_secret(key_name, actor_user_id, *, org_id, accessed_by):
        calls.append(
            {
                "key_name": key_name,
                "actor_user_id": actor_user_id,
                "org_id": org_id,
                "accessed_by": accessed_by,
            }
        )
        return "vault-token"

    monkeypatch.setenv("SERVICE_TOKEN", "env-token")
    monkeypatch.setattr("brain.systems.vault.get_secret", get_secret)

    value = await read_runtime_secret(
        "SERVICE_TOKEN",
        context=RuntimeSecretContext(actor_user_id="user-1", org_id="org-1"),
        reason="Call a configured first-party integration.",
        requested_by="service_tool",
        access="service",
        allow_env_fallback=True,
    )

    assert value == "vault-token"
    assert calls == [
        {
            "key_name": "SERVICE_TOKEN",
            "actor_user_id": "user-1",
            "org_id": "org-1",
            "accessed_by": "service_tool",
        }
    ]


@pytest.mark.asyncio
async def test_service_runtime_secret_can_fall_back_to_env(monkeypatch):
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, read_runtime_secret

    async def get_secret(key_name, actor_user_id, *, org_id, accessed_by):
        return None

    monkeypatch.setenv("SERVICE_TOKEN", "env-token")
    monkeypatch.setattr("brain.systems.vault.get_secret", get_secret)

    value = await read_runtime_secret(
        "SERVICE_TOKEN",
        context=RuntimeSecretContext(actor_user_id="user-1", org_id="org-1"),
        reason="Call a configured first-party integration.",
        requested_by="service_tool",
        access="service",
        allow_env_fallback=True,
    )

    assert value == "env-token"


def _string_arg(node: ast.Call) -> str | None:
    if not node.args:
        return None
    arg = node.args[0]
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _is_os_getenv(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "getenv"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def _is_os_environ_get(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "get"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "environ"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "os"
    )


def _is_os_environ_subscript(node: ast.Subscript) -> str | None:
    if not (
        isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "os"
    ):
        return None
    key = node.slice
    return key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None


def test_first_party_tool_handlers_do_not_read_secret_env_vars_directly():
    offenders: list[str] = []
    paths = [
        *(ROOT / "brain" / "systems" / "runs" / "tool_catalog" / "handlers").glob("*.py"),
        ROOT / "brain" / "app" / "web" / "research.py",
        ROOT / "brain" / "systems" / "workspace_apps" / "generic_http.py",
    ]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (_is_os_getenv(node) or _is_os_environ_get(node)):
                key = _string_arg(node)
                if key and SENSITIVE_ENV_RE.search(key):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} reads {key}")
            elif isinstance(node, ast.Subscript):
                key = _is_os_environ_subscript(node)
                if key and SENSITIVE_ENV_RE.search(key):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} reads {key}")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name.endswith("_from_env"):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {alias.name}")

    assert offenders == []


def test_first_party_runtime_integrations_use_runtime_secret_resolver_for_vault_reads():
    offenders: list[str] = []
    paths = [
        ROOT / "brain" / "systems" / "workspace_apps" / "generic_http.py",
        ROOT / "brain" / "systems" / "slack" / "connector.py",
        ROOT / "brain" / "app" / "web" / "research.py",
    ]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "brain.systems.vault":
                imported = {alias.name for alias in node.names}
                if "get_secret" in imported or "async_get_secret" in imported:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} imports direct Vault secret reads")

    assert offenders == []
