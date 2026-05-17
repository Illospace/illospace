<script lang="ts">
  import type { Snippet } from 'svelte';

  export type ConstellationPanelAs = 'article' | 'aside' | 'div' | 'section';
  export type ConstellationPanelTone = 'default' | 'info' | 'success' | 'warning' | 'danger';
  export type ConstellationPanelPadding = 'none' | 'sm' | 'md' | 'lg';

  type Props = {
    as?: ConstellationPanelAs;
    tone?: ConstellationPanelTone;
    padding?: ConstellationPanelPadding;
    interactive?: boolean;
    className?: string;
    style?: string;
    ariaLabel?: string;
    header?: Snippet;
    footer?: Snippet;
    children?: Snippet;
  };

  let {
    as = 'section',
    tone = 'default',
    padding = 'md',
    interactive = false,
    className = '',
    style = '',
    ariaLabel,
    header,
    footer,
    children,
  }: Props = $props();

  const hasHeader = $derived(Boolean(header));
  const hasFooter = $derived(Boolean(footer));
  const hasContent = $derived(Boolean(children));
  const rootClass = $derived(
    [
      'constellation-panel',
      `constellation-panel-tone-${tone}`,
      `constellation-panel-padding-${padding}`,
      interactive ? 'is-interactive' : '',
      hasHeader ? 'has-header' : '',
      hasContent ? 'has-content' : '',
      hasFooter ? 'has-footer' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<svelte:element this={as} class={rootClass} {style} aria-label={ariaLabel}>
  {#if header}
    <div class="constellation-panel-header">
      {@render header()}
    </div>
  {/if}

  {#if children}
    <div class="constellation-panel-content">
      {@render children()}
    </div>
  {/if}

  {#if footer}
    <div class="constellation-panel-footer">
      {@render footer()}
    </div>
  {/if}
</svelte:element>

<style>
  .constellation-panel {
    --panel-accent: var(--constellation-panel-accent, var(--constellation-tone-info-accent));
    --panel-accent-strong: var(--constellation-panel-accent-strong, var(--constellation-tone-info-accent-strong));
    --panel-accent-soft: var(--constellation-panel-accent-soft, var(--constellation-tone-info-accent-soft));
    --panel-glow-secondary: var(--constellation-tone-warning-accent-soft);
    --panel-separator: var(--constellation-surface-panel-separator);
    --panel-padding: 18px;
    position: relative;
    isolation: isolate;
    display: grid;
    width: 100%;
    min-width: 0;
    overflow: hidden;
    border-radius: var(--constellation-radius-panel);
    border: 1px solid color-mix(in srgb, var(--panel-accent) 28%, var(--constellation-surface-panel-border));
    background:
      radial-gradient(circle at 16% 0%, var(--panel-accent-soft), transparent 34%),
      radial-gradient(circle at 88% 12%, var(--panel-glow-secondary), transparent 32%),
      var(--constellation-surface-panel-background);
    box-shadow: var(--constellation-surface-panel-shadow);
    backdrop-filter: blur(16px) saturate(1.04);
    -webkit-backdrop-filter: blur(16px) saturate(1.04);
    transition:
      transform var(--constellation-motion-hover-duration) var(--constellation-motion-ease-lift),
      border-color var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      box-shadow var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift);
  }

  .constellation-panel::before,
  .constellation-panel::after {
    content: '';
    position: absolute;
    inset: 0;
    pointer-events: none;
  }

  .constellation-panel::before {
    background: var(--constellation-surface-panel-highlight);
    opacity: 0.9;
  }

  .constellation-panel::after {
    inset: auto -12% -42% auto;
    width: 54%;
    height: 66%;
    border-radius: 50%;
    background: radial-gradient(circle, var(--panel-accent-strong), transparent 70%);
    filter: blur(36px);
    opacity: 0.5;
  }

  .constellation-panel.is-interactive:hover {
    transform: translateY(-1px);
    border-color: color-mix(in srgb, var(--panel-accent) 42%, var(--constellation-surface-panel-hover-border));
    box-shadow: var(--constellation-surface-panel-hover-shadow);
  }

  .constellation-panel-header,
  .constellation-panel-content,
  .constellation-panel-footer {
    position: relative;
    z-index: 1;
    min-width: 0;
  }

  .constellation-panel-header,
  .constellation-panel-content,
  .constellation-panel-footer {
    padding: var(--panel-padding);
  }

  .constellation-panel.has-header.has-content .constellation-panel-header,
  .constellation-panel.has-header.has-footer .constellation-panel-header {
    border-bottom: 1px solid var(--panel-separator);
  }

  .constellation-panel.has-footer .constellation-panel-footer {
    border-top: 1px solid var(--panel-separator);
  }

  .constellation-panel-padding-none {
    --panel-padding: 0px;
  }

  .constellation-panel-padding-sm {
    --panel-padding: 14px;
  }

  .constellation-panel-padding-md {
    --panel-padding: 18px;
  }

  .constellation-panel-padding-lg {
    --panel-padding: 22px;
  }

  .constellation-panel-tone-default {
    --panel-accent: var(--constellation-panel-accent, var(--constellation-tone-info-accent));
    --panel-accent-strong: var(--constellation-panel-accent-strong, var(--constellation-tone-info-accent-strong));
    --panel-accent-soft: var(--constellation-panel-accent-soft, var(--constellation-tone-info-accent-soft));
    --panel-glow-secondary: var(--constellation-tone-warning-accent-soft);
  }

  .constellation-panel-tone-info {
    --panel-accent: var(--constellation-tone-info-accent);
    --panel-accent-strong: var(--constellation-tone-info-accent-strong);
    --panel-accent-soft: var(--constellation-tone-info-accent-soft);
    --panel-glow-secondary: var(--constellation-tone-info-accent-soft);
  }

  .constellation-panel-tone-success {
    --panel-accent: var(--constellation-tone-success-accent);
    --panel-accent-strong: var(--constellation-tone-success-accent-strong);
    --panel-accent-soft: var(--constellation-tone-success-accent-soft);
    --panel-glow-secondary: var(--constellation-tone-success-accent-soft);
  }

  .constellation-panel-tone-warning {
    --panel-accent: var(--constellation-tone-warning-accent);
    --panel-accent-strong: var(--constellation-tone-warning-accent-strong);
    --panel-accent-soft: var(--constellation-tone-warning-accent-soft);
    --panel-glow-secondary: var(--constellation-tone-warning-accent-soft);
  }

  .constellation-panel-tone-danger {
    --panel-accent: var(--constellation-tone-danger-accent);
    --panel-accent-strong: var(--constellation-tone-danger-accent-strong);
    --panel-accent-soft: var(--constellation-tone-danger-accent-soft);
    --panel-glow-secondary: var(--constellation-tone-danger-accent-soft);
  }
</style>
