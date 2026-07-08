from __future__ import annotations

import ast
from pathlib import Path
import re
from types import SimpleNamespace

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

    async def async_get_secret_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", async_get_secret_record)
    monkeypatch.setattr(
        "brain.systems.vault.agent_access.read_agent_secret_for_runtime",
        read_agent_secret_for_runtime_with_key,
    )

    value = await read_runtime_secret(
        "GITHUB_TOKEN",
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

    async def async_get_secret_record(*_args, **_kwargs):
        return None

    monkeypatch.setenv("SERVICE_TOKEN", "env-token")
    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", async_get_secret_record)
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

    async def async_get_secret_record(*_args, **_kwargs):
        return None

    monkeypatch.setenv("SERVICE_TOKEN", "env-token")
    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", async_get_secret_record)
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


@pytest.mark.asyncio
async def test_service_runtime_secret_denies_github_app_secret_before_decrypt(monkeypatch):
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, RuntimeSecretUnavailable, read_runtime_secret

    calls = []

    async def async_get_secret_record(key_name, actor_user_id, *, org_id):
        return SimpleNamespace(category="github_app")

    async def get_secret(key_name, actor_user_id, *, org_id, accessed_by):
        calls.append(key_name)
        return "-----BEGIN RSA PRIVATE KEY-----\nsecret-key-material\n-----END RSA PRIVATE KEY-----"

    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", async_get_secret_record)
    monkeypatch.setattr("brain.systems.vault.get_secret", get_secret)

    with pytest.raises(RuntimeSecretUnavailable) as exc:
        await read_runtime_secret(
            "GITHUB_APP__ILLO",
            context=RuntimeSecretContext(actor_user_id="user-1", org_id="org-1"),
            reason="Service callers must not read GitHub App private keys.",
            requested_by="service_tool",
            access="service",
        )

    assert calls == []
    assert "GitHub App credential" in str(exc.value)
    assert "secret-key-material" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_access_level", ["available", "ask"])
async def test_agent_tool_runtime_secret_denies_github_app_secret_before_agent_read(
    monkeypatch,
    agent_access_level,
):
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, RuntimeSecretUnavailable, read_runtime_secret

    raw_blob = (
        '{"app_id":"123","installation_id":"456",'
        '"private_key_pem":"-----BEGIN RSA PRIVATE KEY-----\\nsecret-key-material\\n-----END RSA PRIVATE KEY-----"}'
    )
    read_calls = []

    async def async_get_secret_record(key_name, actor_user_id, *, org_id):
        return SimpleNamespace(category="github_app", agent_access_level=agent_access_level)

    async def read_agent_secret_for_runtime(key_name, **kwargs):
        read_calls.append(key_name)
        return {"value": raw_blob}

    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", async_get_secret_record)
    monkeypatch.setattr(
        "brain.systems.vault.agent_access.read_agent_secret_for_runtime",
        read_agent_secret_for_runtime,
    )

    with pytest.raises(RuntimeSecretUnavailable) as exc:
        await read_runtime_secret(
            "GITHUB_APP__ILLO",
            context=RuntimeSecretContext(
                actor_user_id="user-1",
                org_id="org-1",
                run_id=42,
            ),
            reason="Agent-controlled secret_env mount must not expose GitHub App credentials.",
            requested_by="secret_env_mount",
            access="agent_tool",
        )

    assert read_calls == []
    assert "cannot be read by agents" in str(exc.value)
    assert "secret-key-material" not in str(exc.value)
    assert raw_blob not in str(exc.value)


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


def test_clean_key_name_preserves_vault_key_case():
    from brain.systems.vault.runtime_secrets import _clean_key_name

    assert _clean_key_name("DataForSeoLogin") == "DataForSeoLogin"


def test_vault_key_candidates_preserve_exact_key_before_uppercase_alias():
    from brain.systems.vault.runtime_secrets import _vault_key_candidates

    assert _vault_key_candidates("DataForSeoLogin") == ("DataForSeoLogin", "DATAFORSEOLOGIN")
    assert _vault_key_candidates("GITHUB_TOKEN") == ("GITHUB_TOKEN",)


def test_clean_env_names_includes_uppercase_fallback_without_changing_key():
    from brain.systems.vault.runtime_secrets import _clean_env_names

    assert _clean_env_names("DataForSeoLogin", None) == ("DataForSeoLogin", "DATAFORSEOLOGIN")


@pytest.mark.asyncio
async def test_agent_tool_runtime_secret_falls_back_to_uppercase_legacy_key_when_exact_missing(monkeypatch):
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, read_runtime_secret

    record_calls = []
    read_calls = []

    async def async_get_secret_record(key_name, actor_user_id, *, org_id):
        record_calls.append((key_name, actor_user_id, org_id))
        return object() if key_name == "DATAFORSEOLOGIN" else None

    async def read_agent_secret_for_runtime(key_name, **kwargs):
        read_calls.append(key_name)
        return {"key": key_name, "value": "legacy-uppercase-value"}

    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", async_get_secret_record)
    monkeypatch.setattr(
        "brain.systems.vault.agent_access.read_agent_secret_for_runtime",
        read_agent_secret_for_runtime,
    )

    value = await read_runtime_secret(
        "DataForSeoLogin",
        context=RuntimeSecretContext(actor_user_id="user-1", org_id="org-1", run_id=42),
        reason="Run a bounded DataForSEO SERP check.",
        requested_by="secret_env_mount",
        access="agent_tool",
    )

    assert value == "legacy-uppercase-value"
    assert record_calls == [
        ("DataForSeoLogin", "user-1", "org-1"),
        ("DATAFORSEOLOGIN", "user-1", "org-1"),
        # agent_tool github_app guard re-reads the selected candidate's record.
        ("DATAFORSEOLOGIN", "user-1", "org-1"),
    ]
    assert read_calls == ["DATAFORSEOLOGIN"]


@pytest.mark.asyncio
async def test_agent_tool_runtime_secret_does_not_fall_back_on_permission_error(monkeypatch):
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, RuntimeSecretUnavailable, read_runtime_secret

    calls = []

    async def async_get_secret_record(key_name, actor_user_id, *, org_id):
        return object() if key_name == "DataForSeoLogin" else None

    async def read_agent_secret_for_runtime(key_name, **kwargs):
        calls.append(key_name)
        return {"error": "secret is marked manual and cannot be auto-read by agents"}

    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", async_get_secret_record)
    monkeypatch.setattr(
        "brain.systems.vault.agent_access.read_agent_secret_for_runtime",
        read_agent_secret_for_runtime,
    )

    with pytest.raises(RuntimeSecretUnavailable, match="marked manual"):
        await read_runtime_secret(
            "DataForSeoLogin",
            context=RuntimeSecretContext(actor_user_id="user-1", org_id="org-1", run_id=42),
            reason="Run a bounded DataForSEO SERP check.",
            requested_by="secret_env_mount",
            access="agent_tool",
        )

    assert calls == ["DataForSeoLogin"]


@pytest.mark.asyncio
async def test_service_runtime_secret_falls_back_to_uppercase_legacy_vault_key(monkeypatch):
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, read_runtime_secret

    record_calls = []
    calls = []

    async def async_get_secret_record(key_name, actor_user_id, *, org_id):
        record_calls.append((key_name, actor_user_id, org_id))
        return object() if key_name == "SERVICE_TOKEN" else None

    async def get_secret(key_name, actor_user_id, *, org_id, accessed_by):
        calls.append((key_name, actor_user_id, org_id, accessed_by))
        return "legacy-service-token" if key_name == "SERVICE_TOKEN" else None

    monkeypatch.setattr("brain.systems.vault.async_get_secret_record", async_get_secret_record)
    monkeypatch.setattr("brain.systems.vault.get_secret", get_secret)

    value = await read_runtime_secret(
        "service_token",
        context=RuntimeSecretContext(actor_user_id="user-1", org_id="org-1"),
        reason="Call a configured first-party integration.",
        requested_by="service_tool",
        access="service",
    )

    assert value == "legacy-service-token"
    assert record_calls == [
        ("service_token", "user-1", "org-1"),
        ("SERVICE_TOKEN", "user-1", "org-1"),
        ("SERVICE_TOKEN", "user-1", "org-1"),
    ]
    assert calls == [("SERVICE_TOKEN", "user-1", "org-1", "service_tool")]
