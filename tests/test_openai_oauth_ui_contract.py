from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_onboarding_shows_manual_callback_escape_hatch_while_oauth_is_pending():
    source = (ROOT / "frontend/src/routes/onboarding/+page.svelte").read_text()

    assert "const oauthPending = $derived(Boolean(oauthUrl || oauthState || status === 'connecting'));" in source
    assert "const showManualCallback = $derived(Boolean(status !== 'connected' && oauthPending));" in source
    assert 'href={oauthUrl}' in source
    assert 'target="_blank"' in source
    assert "captureManualCallback(data.callback);" in source
    assert "await completeOpenAI(data.callback);" not in source
    assert "!oauthCallbackAvailable &&" not in source
    assert "window.location.assign(oauthUrl)" not in source


def test_system_access_card_expands_manual_callback_while_oauth_is_pending():
    source = (ROOT / "frontend/src/routes/system/AccessCard.svelte").read_text()

    assert "const showManualCallback = $derived(Boolean(oauthPending));" in source
    assert "{#if showManualCallback}" in source
    assert "oauthUrl: string;" in source
    assert 'href={oauthUrl}' in source


def test_system_oauth_start_keeps_page_open_when_popup_is_blocked():
    source = (ROOT / "frontend/src/routes/system/+page.svelte").read_text()

    assert "captureManualCodexCallback(data.callback);" in source
    assert "void completeCodexSignIn(data.callback);" not in source
    assert "window.location.assign(oauthUrl)" not in source


def test_openai_callback_page_does_not_auto_open_cortex():
    source = (ROOT / "frontend/src/routes/auth/OpenAIOAuthCallback.svelte").read_text()

    assert "Opening Cortex" not in source
    assert "window.location.assign" not in source
    assert "schedulePopupClose();" in source


def test_onboarding_routes_require_personal_openai_connection():
    helper = (ROOT / "frontend/src/lib/utils/runtimeOnboarding.ts").read_text()
    login = (ROOT / "frontend/src/routes/login/+page.svelte").read_text()
    layout = (ROOT / "frontend/src/routes/+layout.svelte").read_text()
    onboarding = (ROOT / "frontend/src/routes/onboarding/+page.svelte").read_text()

    assert "connection?.source === 'user_default'" in helper
    assert "requiresPersonalOpenAIOnboarding(runtime)" in login
    assert "requiresPersonalOpenAIOnboarding(runtime)" in layout
    assert "hasPersonalOpenAIRuntimeConnection(runtime)" in onboarding
    assert "org_main'].includes" not in onboarding
