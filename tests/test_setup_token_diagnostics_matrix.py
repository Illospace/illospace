from unittest.mock import patch


def test_build_cases_covers_core_hypotheses():
    from tests.manual_setup_token_diagnostics import _build_cases

    with patch("tests.manual_setup_token_diagnostics.get_oauth_betas", return_value=["claude-code-20250219", "oauth-2025-04-20"]):
        cases = _build_cases()

    labels = {case.label for case in cases}
    assert "plain_auth_token_no_headers" in labels
    assert "plain_auth_token_x_app_default_headers" in labels
    assert "plain_auth_token_core_betas_extra_headers" in labels
    assert "plain_auth_token_full_betas_extra_headers" in labels
    assert "shared_adapter_minimal" in labels
    assert "shared_adapter_adaptive_no_tools" in labels
    assert "shared_adapter_adaptive_tools" in labels
    assert "shared_adapter_enabled_no_tools" in labels
    assert "shared_adapter_enabled_tools" in labels
