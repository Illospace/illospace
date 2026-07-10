<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import type { CortexWorkspaceComposerSettingsGroup } from '$lib/features/composer/domain/composerAdapter';

  let {
    groups,
    ariaLabel,
    onSettingsChange,
  }: {
    groups: readonly CortexWorkspaceComposerSettingsGroup[];
    ariaLabel: string;
    onSettingsChange?: (key: string, value: string) => void;
  } = $props();

  let activeGroupKey = $state<string | null>(null);

  $effect(() => {
    const hasActiveGroup = groups.some((group) => group.key === activeGroupKey);
    if (!activeGroupKey || !hasActiveGroup) activeGroupKey = groups[0]?.key ?? null;
  });

  function selectedOption(group: CortexWorkspaceComposerSettingsGroup) {
    return group.options.find((option) => option.value === group.value) ?? group.options[0] ?? null;
  }

  function selectedLabel(group: CortexWorkspaceComposerSettingsGroup) {
    const option = selectedOption(group);
    return option ? `${group.label}: ${option.label}` : group.label;
  }
</script>

<div role="menu" class="composer-settings-menu" aria-label={ariaLabel}>
  <div class="composer-settings-primary" role="group" aria-label="Run setting categories">
    {#each groups as group (group.key)}
      <button
        type="button"
        class:composer-settings-group-trigger={true}
        class:is-active={activeGroupKey === group.key}
        role="menuitem"
        aria-haspopup="menu"
        aria-expanded={activeGroupKey === group.key}
        onpointerenter={() => (activeGroupKey = group.key)}
        onfocus={() => (activeGroupKey = group.key)}
        onclick={() => (activeGroupKey = group.key)}
      >
        <span class="composer-settings-group-label">{group.label}</span>
        <span class="composer-settings-group-value">{selectedOption(group)?.label ?? ''}</span>
      </button>
    {/each}
  </div>

  <div class="composer-settings-secondary">
    {#each groups as group (group.key)}
      {@const isActiveGroup = activeGroupKey === group.key}
      <div
        class:composer-settings-group-panel={true}
        class:is-active={isActiveGroup}
        aria-label={group.ariaLabel ?? group.label}
        aria-hidden={isActiveGroup ? undefined : 'true'}
      >
        <div class="composer-settings-heading">{selectedLabel(group)}</div>
        <div class="composer-settings-options">
          {#each group.options as option}
            {@const isActive = option.value === (selectedOption(group)?.value ?? '')}
            <button
              type="button"
              role="menuitemradio"
              aria-checked={isActive}
              title={option.description ?? option.label}
              class:composer-settings-option={true}
              class:is-active={isActive}
              tabindex={isActiveGroup ? 0 : -1}
              onclick={() => onSettingsChange?.(group.key, option.value)}
            >
              <span class="composer-settings-option-main">
                {#if option.icon}
                  <ConstellationIcon name={option.icon} size={14} stroke={1.9} />
                {/if}
                <span class="composer-settings-option-copy">
                  <span class="composer-settings-option-label">{option.label}</span>
                  {#if option.description}
                    <span class="composer-settings-option-description">{option.description}</span>
                  {/if}
                </span>
              </span>
              <span class="composer-settings-option-indicator" aria-hidden="true"></span>
            </button>
          {/each}
        </div>
      </div>
    {/each}
  </div>
</div>

<style>
  .composer-settings-menu {
    position: absolute;
    left: 0;
    bottom: calc(100% + 8px);
    z-index: 32;
    display: grid;
    grid-template-columns: 128px minmax(220px, 1fr);
    gap: 6px;
    width: min(386px, calc(100vw - 32px));
    max-width: min(520px, calc(100vw - 32px));
    padding: 7px;
    border: 1px solid var(--composer-menu-border);
    border-radius: 14px;
    background: var(--composer-menu-background);
    box-shadow: var(--composer-menu-shadow);
    color: var(--constellation-color-text-primary);
  }

  .composer-settings-primary,
  .composer-settings-secondary {
    min-width: 0;
  }

  .composer-settings-primary {
    display: grid;
    align-content: start;
    gap: 3px;
    padding-right: 5px;
    border-right: 1px solid color-mix(in oklab, var(--composer-menu-border), transparent 38%);
  }

  .composer-settings-secondary {
    position: relative;
    display: grid;
    align-items: start;
    min-height: 150px;
  }

  .composer-settings-group-trigger {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    column-gap: 12px;
    min-height: 32px;
    padding: 6px 7px;
    border: 0;
    border-radius: 9px;
    background: transparent;
    color: var(--constellation-select-chip-option-text);
    font: inherit;
    cursor: pointer;
    text-align: left;
  }

  .composer-settings-group-trigger:hover,
  .composer-settings-group-trigger.is-active {
    background: var(--composer-menu-option-hover-background);
  }

  .composer-settings-group-trigger.is-active {
    color: var(--constellation-select-chip-trigger-hover-text);
  }

  .composer-settings-group-trigger:focus-visible,
  .composer-settings-option:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .composer-settings-group-label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 12px;
    font-weight: 650;
  }

  .composer-settings-group-value {
    max-width: 78px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--composer-menu-supporting-text);
    font-size: 10.5px;
  }

  .composer-settings-group-panel {
    display: grid;
    gap: 4px;
    grid-area: 1 / 1;
    min-width: 0;
    opacity: 0;
    pointer-events: none;
    visibility: hidden;
  }

  .composer-settings-group-panel.is-active {
    opacity: 1;
    pointer-events: auto;
    visibility: visible;
  }

  .composer-settings-heading {
    padding: 0 4px;
    color: var(--composer-menu-supporting-text);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .composer-settings-options {
    display: grid;
    gap: 3px;
  }

  .composer-settings-option {
    display: inline-flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 34px;
    padding: 7px 8px;
    border: 0;
    border-radius: 9px;
    background: transparent;
    color: var(--constellation-select-chip-option-text);
    font: inherit;
    font-size: 12px;
    cursor: pointer;
    text-align: left;
  }

  .composer-settings-option:hover {
    background: var(--composer-menu-option-hover-background);
  }

  .composer-settings-option.is-active {
    background: var(--composer-menu-option-active-background);
  }

  .composer-settings-option-main {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }

  .composer-settings-option-copy {
    display: grid;
    gap: 1px;
    min-width: 0;
  }

  .composer-settings-option-label,
  .composer-settings-option-description {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .composer-settings-option-label {
    font-weight: 650;
  }

  .composer-settings-option-description {
    color: var(--composer-menu-supporting-text);
    font-size: 10.5px;
    line-height: 1.2;
  }

  .composer-settings-option-indicator {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: transparent;
    flex-shrink: 0;
  }

  .composer-settings-option.is-active .composer-settings-option-indicator {
    background: var(--constellation-select-chip-indicator-active-background);
  }

  @container composer-controls (max-width: 230px) {
    .composer-settings-menu {
      left: auto;
      right: 0;
    }
  }
</style>
