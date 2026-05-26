import { appCapsuleBridgeScript, type AppCapsuleRuntimeApp } from './appCapsuleBridge';
import { appCapsuleRuntimeStyle } from './appCapsuleStyle';
import { buildWorkspaceAppSrcdoc, escapeHtml, stableSignature } from './workspaceAppRuntime';

export type { AppCapsuleRuntimeApp };
export { stableSignature };

export type AppCapsuleSrcdocOptions = {
  source: string;
  title: string;
  manifest: Record<string, any>;
  themeMode: 'dark' | 'light';
  accent: string;
  app: AppCapsuleRuntimeApp;
};

export function fallbackAppCapsuleSource(appName: string) {
  return `
    <main class="illo-app">
      <section class="illo-panel illo-stack">
        <div class="illo-toolbar">
          <h1 class="illo-title">${escapeHtml(appName)}</h1>
          <span class="illo-badge">Ready</span>
        </div>
        <div class="illo-empty">
          <p>This app capsule has no source yet.</p>
        </div>
      </section>
    </main>
  `;
}

export function buildAppCapsuleSrcdoc(options: AppCapsuleSrcdocOptions) {
  return buildWorkspaceAppSrcdoc({
    source: options.source,
    title: options.title,
    manifest: options.manifest,
    runtimeStyle: appCapsuleRuntimeStyle(options.themeMode, options.accent),
    bridgeScript: appCapsuleBridgeScript(options.app),
  });
}
