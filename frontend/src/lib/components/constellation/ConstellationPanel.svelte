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
    --panel-accent: rgba(141, 183, 255, 0.22);
    --panel-accent-strong: rgba(141, 183, 255, 0.16);
    --panel-accent-soft: rgba(141, 183, 255, 0.07);
    --panel-glow-secondary: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 8%, transparent);
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
    --panel-accent: rgba(141, 183, 255, 0.22);
    --panel-accent-strong: rgba(141, 183, 255, 0.15);
    --panel-accent-soft: rgba(141, 183, 255, 0.07);
    --panel-glow-secondary: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 8%, transparent);
  }

  .constellation-panel-tone-info {
    --panel-accent: rgba(141, 183, 255, 0.28);
    --panel-accent-strong: rgba(141, 183, 255, 0.18);
    --panel-accent-soft: rgba(141, 183, 255, 0.1);
    --panel-glow-secondary: rgba(141, 183, 255, 0.08);
  }

  .constellation-panel-tone-success {
    --panel-accent: rgba(109, 245, 189, 0.24);
    --panel-accent-strong: rgba(109, 245, 189, 0.16);
    --panel-accent-soft: rgba(109, 245, 189, 0.08);
    --panel-glow-secondary: rgba(87, 207, 160, 0.08);
  }

  .constellation-panel-tone-warning {
    --panel-accent: rgba(250, 231, 188, 0.22);
    --panel-accent-strong: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 17%, transparent);
    --panel-accent-soft: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 9%, transparent);
    --panel-glow-secondary: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 12%, transparent);
  }

  .constellation-panel-tone-danger {
    --panel-accent: rgba(219, 110, 130, 0.24);
    --panel-accent-strong: rgba(219, 110, 130, 0.16);
    --panel-accent-soft: rgba(219, 110, 130, 0.08);
    --panel-glow-secondary: rgba(219, 110, 130, 0.1);
  }
</style>
