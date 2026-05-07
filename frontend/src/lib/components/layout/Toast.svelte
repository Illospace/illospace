<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import type { ConstellationIconName } from '$lib/components/constellation';
  import { ui } from '$lib/stores/ui.svelte';

  type ToastTone = 'info' | 'error' | 'success';

  const iconByTone: Record<ToastTone, ConstellationIconName> = {
    info: 'notification',
    success: 'check',
    error: 'x',
  };
</script>

{#if ui.toasts.length > 0}
  <div class="constellation-toast-region" aria-live="polite">
    {#each ui.toasts as t (t.id)}
      <div
        class="constellation-toast constellation-toast--{t.type}"
        role={t.type === 'error' ? 'alert' : 'status'}
        aria-atomic="true"
      >
        <span class="constellation-toast__icon" aria-hidden="true">
          <ConstellationIcon name={iconByTone[t.type]} size={14} stroke={2} />
        </span>
        <span class="constellation-toast__message">{t.text}</span>
        <button
          class="constellation-toast__dismiss"
          type="button"
          aria-label="Dismiss notification"
          onclick={() => ui.dismiss(t.id)}
        >
          <ConstellationIcon name="x" size={13} stroke={2} />
        </button>
      </div>
    {/each}
  </div>
{/if}

<style>
  .constellation-toast-region {
    position: fixed;
    right: max(20px, env(safe-area-inset-right));
    bottom: max(20px, env(safe-area-inset-bottom));
    z-index: var(--z-toast, 1000);
    display: grid;
    gap: 8px;
    width: min(360px, calc(100vw - 32px));
    pointer-events: none;
  }

  .constellation-toast {
    --toast-accent-background: var(--constellation-control-pill-info-background);
    --toast-accent-border: var(--constellation-control-pill-info-border);
    --toast-accent-text: var(--constellation-control-pill-info-text);

    pointer-events: auto;
    position: relative;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    min-height: 44px;
    padding: 9px 10px 9px 12px;
    overflow: hidden;
    color: var(--constellation-color-text-primary);
    background: var(--constellation-surface-floating-background);
    border: 1px solid var(--constellation-surface-floating-border);
    border-radius: calc(var(--constellation-radius-panel) - 4px);
    box-shadow: var(--constellation-surface-floating-shadow);
    backdrop-filter: blur(18px) saturate(1.04);
    -webkit-backdrop-filter: blur(18px) saturate(1.04);
    animation: constellation-toast-enter var(--constellation-motion-settle-duration, 240ms)
      var(--constellation-motion-ease-lift, cubic-bezier(0.22, 1, 0.36, 1));
  }

  .constellation-toast::before {
    content: '';
    position: absolute;
    inset: 0;
    pointer-events: none;
    background:
      var(--constellation-surface-floating-highlight),
      linear-gradient(90deg, var(--toast-accent-background), transparent 42%);
    opacity: 0.78;
  }

  .constellation-toast--success {
    --toast-accent-background: var(--constellation-control-pill-success-background);
    --toast-accent-border: var(--constellation-control-pill-success-border);
    --toast-accent-text: var(--constellation-control-pill-success-text);
  }

  .constellation-toast--error {
    --toast-accent-background: var(--constellation-control-pill-danger-background);
    --toast-accent-border: var(--constellation-control-pill-danger-border);
    --toast-accent-text: var(--constellation-control-pill-danger-text);
  }

  .constellation-toast__icon,
  .constellation-toast__message,
  .constellation-toast__dismiss {
    position: relative;
    z-index: 1;
  }

  .constellation-toast__icon {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    color: var(--toast-accent-text);
    background: var(--toast-accent-background);
    border: 1px solid var(--toast-accent-border);
    border-radius: var(--constellation-radius-pill);
  }

  .constellation-toast__message {
    min-width: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-size: var(--constellation-type-body-sm);
    font-weight: 520;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }

  .constellation-toast__dismiss {
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    padding: 0;
    color: var(--constellation-color-text-secondary);
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--constellation-radius-pill);
    cursor: pointer;
    transition:
      color var(--constellation-motion-hover-duration, 180ms) ease,
      background var(--constellation-motion-hover-duration, 180ms) ease,
      border-color var(--constellation-motion-hover-duration, 180ms) ease;
  }

  .constellation-toast__dismiss:hover {
    color: var(--constellation-color-text-primary);
    background: var(--constellation-control-pill-background);
    border-color: var(--constellation-control-pill-border);
  }

  .constellation-toast__dismiss:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  @keyframes constellation-toast-enter {
    from {
      opacity: 0;
      transform: translate3d(0, 8px, 0) scale(0.985);
    }
    to {
      opacity: 1;
      transform: translate3d(0, 0, 0) scale(1);
    }
  }

  @media (max-width: 640px) {
    .constellation-toast-region {
      right: max(12px, env(safe-area-inset-right));
      bottom: max(12px, env(safe-area-inset-bottom));
      left: max(12px, env(safe-area-inset-left));
      width: auto;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .constellation-toast {
      animation: none;
    }

    .constellation-toast__dismiss {
      transition: none;
    }
  }
</style>
