from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_onboarding_shows_manual_callback_escape_hatch_while_oauth_is_pending():
    source = (ROOT / "frontend/src/routes/onboarding/+page.svelte").read_text()

    assert "const oauthPending = $derived(Boolean(oauthUrl || oauthState || status === 'connecting'));" in source
    assert "const showManualCallback = $derived(Boolean(status !== 'connected' && oauthPending));" in source
    assert 'href={oauthUrl}' in source
    assert 'target="_blank"' in source
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

    assert "window.location.assign(oauthUrl)" not in source
