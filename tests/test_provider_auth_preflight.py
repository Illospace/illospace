from __future__ import annotations

import pytest

from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthBlockedPreflightResult,
    ProviderAuthPassedPreflightResult,
)


def test_auth_variants_fix_their_status_and_preserve_serialization():
    blocked = ProviderAuthBlockedPreflightResult(
        provider="anthropic",
        model="anthropic/claude-sonnet-4-6",
        credential="Anthropic API key",
        error_code="provider_credential_unavailable",
    )

    assert blocked.to_dict() == {
        "status": "auth_blocked",
        "provider": "anthropic",
        "model": "anthropic/claude-sonnet-4-6",
        "credential": "Anthropic API key",
        "error_code": "provider_credential_unavailable",
        "repair_action": None,
        "visible_message": None,
    }
    with pytest.raises(TypeError):
        ProviderAuthPassedPreflightResult(
            status="auth_blocked",
            provider="anthropic",
            model="anthropic/claude-sonnet-4-6",
        )
