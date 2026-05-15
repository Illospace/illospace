import pytest

from brain.systems.vault import (
    DEFAULT_VAULT_AGENT_ACCESS_LEVEL,
    VAULT_ACCESS_ACTORS,
    VAULT_AGENT_ACCESS_LEVELS,
    _normalize_accessed_by,
    _normalize_env_name,
    _normalize_project_slug,
    normalize_agent_access_level,
)


def test_vault_access_actor_normalization_preserves_known_actors_and_maps_integrations():
    for actor in VAULT_ACCESS_ACTORS:
        assert _normalize_accessed_by(actor.upper()) == actor

    assert _normalize_accessed_by(None) == "user"
    assert _normalize_accessed_by("github_connector") == "api"
    assert _normalize_accessed_by("unknown-ci-system") == "api"


def test_vault_agent_access_levels_are_explicit_not_silent():
    for level in VAULT_AGENT_ACCESS_LEVELS:
        assert normalize_agent_access_level(level.upper()) == level

    assert normalize_agent_access_level(None) == DEFAULT_VAULT_AGENT_ACCESS_LEVEL
    with pytest.raises(ValueError):
        normalize_agent_access_level("always")


def test_project_secret_binding_normalization_is_stable_and_env_safe():
    assert _normalize_project_slug("  My-App ") == "my-app"
    assert _normalize_env_name(" OPENAI_API_KEY ") == "OPENAI_API_KEY"
    assert _normalize_env_name("_INTERNAL_TOKEN") == "_INTERNAL_TOKEN"

    for env_name in ("", "1PASSWORD", "OPENAI-KEY", "KEY NAME", "éclair"):
        with pytest.raises(ValueError):
            _normalize_env_name(env_name)
