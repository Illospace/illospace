import { escapeHtml } from './workspaceAppRuntime';

export function appCapsuleRuntimeStyle(themeMode: 'dark' | 'light', accent: string) {
  const dark = themeMode !== 'light';
  return `<style>
    :root {
      color-scheme: ${dark ? 'dark' : 'light'};
      --illo-font: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --illo-accent: ${escapeHtml(accent)};
      --illo-bg: ${dark ? 'rgba(6, 10, 18, 0.96)' : 'rgba(247, 250, 252, 0.96)'};
      --illo-panel: ${dark ? 'rgba(13, 19, 31, 0.94)' : 'rgba(255, 255, 255, 0.96)'};
      --illo-panel-strong: ${dark ? 'rgba(20, 28, 44, 0.98)' : 'rgba(255, 255, 255, 1)'};
      --illo-border: ${dark ? 'rgba(255, 255, 255, 0.12)' : 'rgba(15, 23, 42, 0.12)'};
      --illo-text: ${dark ? 'rgba(244, 247, 251, 0.96)' : 'rgba(15, 23, 42, 0.92)'};
      --illo-muted: ${dark ? 'rgba(226, 232, 240, 0.66)' : 'rgba(71, 85, 105, 0.74)'};
      --illo-soft: ${dark ? 'rgba(255, 255, 255, 0.07)' : 'rgba(15, 23, 42, 0.06)'};
      --illo-danger: ${dark ? '#EF6F7B' : '#C94457'};
      --illo-radius-sm: 6px;
      --illo-radius-md: 8px;
      --illo-control-height: 36px;
      font-family: var(--illo-font);
      background: transparent;
    }

    * { box-sizing: border-box; }

    html,
    body {
      width: 100%;
      min-width: 0;
      min-height: 100%;
      margin: 0;
      overflow: hidden;
      background: transparent;
      color: var(--illo-text);
    }

    body,
    button,
    input,
    textarea,
    select {
      font: inherit;
    }

    button,
    input,
    textarea,
    select {
      color: inherit;
    }

    button { cursor: pointer; }

    button:disabled,
    input:disabled,
    textarea:disabled,
    select:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }

    a { color: var(--illo-accent); }

    :focus-visible {
      outline: 2px solid color-mix(in srgb, var(--illo-accent) 72%, white);
      outline-offset: 2px;
    }

    img,
    canvas,
    svg,
    video {
      max-width: 100%;
    }

    .illo-generated-app-root {
      width: 100%;
      min-height: 100%;
    }

    .illo-app {
      width: 100%;
      min-height: 100vh;
      display: grid;
      gap: 16px;
      padding: clamp(16px, 3vw, 32px);
      color: var(--illo-text);
      font-family: var(--illo-font);
      background: var(--illo-bg);
    }

    .illo-panel {
      min-width: 0;
      border: 1px solid var(--illo-border);
      border-radius: var(--illo-radius-md);
      background: var(--illo-panel);
      overflow: hidden;
    }

    .illo-toolbar,
    .illo-row {
      display: flex;
      min-width: 0;
      align-items: center;
      gap: 10px;
    }

    .illo-toolbar {
      flex-wrap: wrap;
      justify-content: space-between;
    }

    .illo-stack { display: grid; gap: 12px; }
    .illo-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }

    .illo-row {
      padding: 10px 12px;
      border: 1px solid var(--illo-border);
      border-radius: var(--illo-radius-md);
      background: var(--illo-soft);
    }

    .illo-input,
    .illo-select,
    .illo-textarea,
    .illo-app input:not([type='checkbox']):not([type='radio']):not([type='hidden']),
    .illo-app textarea,
    .illo-app select {
      min-width: 0;
      width: 100%;
      border: 1px solid var(--illo-border);
      border-radius: var(--illo-radius-md);
      background: var(--illo-panel-strong);
      color: var(--illo-text);
      padding: 10px 12px;
      outline: none;
    }

    .illo-button,
    .illo-app button {
      min-height: var(--illo-control-height);
      border: 1px solid var(--illo-border);
      border-radius: var(--illo-radius-md);
      background: var(--illo-panel-strong);
      color: var(--illo-text);
      padding: 9px 13px;
      font-weight: 700;
      line-height: 1;
    }

    .illo-button[data-variant='primary'],
    .illo-button.is-primary,
    .illo-button-primary {
      border-color: color-mix(in srgb, var(--illo-accent) 42%, var(--illo-border));
      background: color-mix(in srgb, var(--illo-accent) 18%, var(--illo-panel-strong));
    }

    .illo-button-danger {
      border-color: color-mix(in srgb, var(--illo-danger) 74%, transparent);
      background: var(--illo-danger);
      color: #FFFFFF;
    }

    .illo-tabs {
      display: inline-flex;
      gap: 4px;
      padding: 4px;
      border: 1px solid var(--illo-border);
      border-radius: 999px;
      background: var(--illo-soft);
    }

    .illo-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--illo-accent) 13%, var(--illo-soft));
      color: var(--illo-text);
      padding: 4px 8px;
      font-size: 12px;
      font-weight: 700;
    }

    .illo-title {
      margin: 0;
      color: var(--illo-text);
      font-size: clamp(22px, 4vw, 34px);
      line-height: 1.08;
      letter-spacing: 0;
    }

    .illo-copy,
    .illo-muted,
    .illo-empty {
      color: var(--illo-muted);
    }

    .illo-empty {
      display: grid;
      min-height: 160px;
      place-content: center;
      text-align: center;
    }

    .illo-table-wrap {
      min-width: 0;
      overflow: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
    }

    th,
    td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--illo-border);
      text-align: left;
      vertical-align: top;
    }

    th {
      color: var(--illo-muted);
      font-size: 12px;
      font-weight: 700;
    }
  </style>`;
}
