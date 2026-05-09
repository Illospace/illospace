const OPENAI_OAUTH_POPUP_TARGET = 'illo-openai-oauth';
const OPENAI_OAUTH_POPUP_FEATURES = 'popup,width=540,height=760';

export function openOpenAIOAuthPopup(): Window | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.open('about:blank', OPENAI_OAUTH_POPUP_TARGET, OPENAI_OAUTH_POPUP_FEATURES);
  } catch {
    return null;
  }
}

export function navigateOpenAIOAuthPopup(popup: Window | null, url: string): boolean {
  if (!url) return false;
  try {
    if (!popup || popup.closed) return false;
    popup.location.href = url;
    popup.focus();
    return true;
  } catch {
    return false;
  }
}

export function closeOAuthPopup(popup: Window | null) {
  try {
    if (popup && !popup.closed) popup.close();
  } catch {
    // Best-effort cleanup for a popup that may have crossed origins.
  }
}
