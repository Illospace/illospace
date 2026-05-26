<script lang="ts">
  import type { WorkspaceAppRead } from '$lib/features/workspace-apps/api/workspaceAppsApi';
  import { ConstellationIcon, ConstellationPill } from '$lib/components/constellation';

  import AppCapsuleRenderer from './AppCapsuleRenderer.svelte';
  import GeneratedHtmlAppRuntime from './GeneratedHtmlAppRuntime.svelte';
  import GeneratedUiRenderer from './GeneratedUiRenderer.svelte';

  let {
    app,
    surface = 'workspace',
    onclose,
  }: {
    app: WorkspaceAppRead | null;
    surface?: 'workspace' | 'dock';
    onclose?: () => void;
  } = $props();

  const activeVersion = $derived(app?.active_version ?? null);
  const canRenderAppCapsule = $derived(
    !!app
      && (
        app.renderer_key === 'app-capsule'
        || (activeVersion?.renderer_key === 'app-capsule' && activeVersion?.source_kind === 'html')
      ),
  );
  const canRenderGeneratedUi = $derived(
    !!app
      && (
        app.renderer_key === 'generated-ui-app'
        || activeVersion?.source_kind === 'json'
        || activeVersion?.source_kind === 'generated-ui'
        || activeVersion?.source_kind === 'generated_ui'
      ),
  );
  const canRenderHtml = $derived(
    !!app
      && (
        app.renderer_key === 'sandboxed-html-app'
        || activeVersion?.renderer_key === 'sandboxed-html-app'
      ),
);
</script>

{#if app && canRenderAppCapsule}
  <AppCapsuleRenderer {app} {surface} {onclose} />
{:else if app && canRenderGeneratedUi}
  <GeneratedUiRenderer {app} {surface} {onclose} />
{:else if app && canRenderHtml}
  <GeneratedHtmlAppRuntime {app} {surface} {onclose} />
{:else if app}
  <section class="generated-app-unsupported generated-app-shell" class:is-dock={surface === 'dock'}>
    <div class="generated-app-unsupported__glyph" aria-hidden="true">
      <ConstellationIcon name="code" size={18} stroke={1.9} />
    </div>
    <div class="generated-app-unsupported__copy">
      <ConstellationPill variant="info" leadingDot>{app.renderer_key}</ConstellationPill>
      <h2>{app.name}</h2>
      <p>This generated app was saved, but this client cannot render its source kind yet.</p>
    </div>
  </section>
{/if}

<style>
.generated-app-unsupported {
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr);
  gap: 13px;
  width: min(460px, calc(100vw - 28px));
  min-width: 0;
  padding: 18px;
  border-radius: 22px;
}

.generated-app-unsupported.is-dock {
  width: 100%;
  min-height: 100%;
  border-radius: 0;
}

.generated-app-unsupported__glyph {
  display: inline-flex;
  width: 42px;
  height: 42px;
  align-items: center;
  justify-content: center;
  border-radius: 14px;
  background: var(--constellation-control-pill-info-background);
  color: var(--constellation-control-pill-info-text);
}

.generated-app-unsupported__copy {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.generated-app-unsupported h2 {
  margin: 0;
  color: var(--constellation-section-title);
  font-size: 17px;
  letter-spacing: 0;
}

.generated-app-unsupported p {
  margin: 0;
  color: var(--constellation-section-description);
  font-size: 12px;
  line-height: 1.45;
}

</style>
