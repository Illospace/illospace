<script lang="ts">
  import type { ProjectContextResource } from '$lib/utils/projectContext';
  import { resourceKind, resourceLabel } from './projectContextProfiles';

  let {
    resources = [],
    selected = false,
    removable = false,
    ariaLabel = 'Project context resources',
    onRemove,
  }: {
    resources?: ProjectContextResource[];
    selected?: boolean;
    removable?: boolean;
    ariaLabel?: string;
    onRemove?: (resource: ProjectContextResource) => void;
  } = $props();
</script>

{#if resources.length}
  <div class="project-context-resource-list" class:selected aria-label={ariaLabel}>
    {#each resources as resource}
      {#if removable}
        <button
          type="button"
          class="project-context-resource-pill removable"
          onclick={() => onRemove?.(resource)}
        >
          {resourceKind(resource)} - {resourceLabel(resource)}
        </button>
      {:else}
        <span class="project-context-resource-pill">
          {resourceKind(resource)} - {resourceLabel(resource)}
        </span>
      {/if}
    {/each}
  </div>
{/if}
