<script lang="ts">
  import ConstellationPresenceSeed, {
    type ConstellationPresenceSeedSize,
  } from './ConstellationPresenceSeed.svelte';
  import type { ConstellationTone } from './constellationTypes';

  export type ConstellationPresenceStackMember = {
    name: string;
    tone: ConstellationTone;
    style?: string;
  };

  type Props = {
    members: ConstellationPresenceStackMember[];
    size?: ConstellationPresenceSeedSize;
    caption?: string;
    className?: string;
  };

  let { members, size = 'xs', caption = '', className = '' }: Props = $props();

  const rootClass = $derived(
    ['constellation-presence-stack', className].filter(Boolean).join(' '),
  );
</script>

<div class={rootClass}>
  <div class="constellation-presence-stack-stack" aria-label={`${members.length} active members`}>
    {#each members as member (member.name)}
      <ConstellationPresenceSeed
        label={member.name}
        tone={member.tone}
        style={member.style}
        {size}
      />
    {/each}
  </div>

  {#if caption}
    <span class="constellation-presence-stack-caption">{caption}</span>
  {/if}
</div>

<style>
  .constellation-presence-stack {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .constellation-presence-stack-stack {
    display: flex;
    align-items: center;
  }

  .constellation-presence-stack-stack > :global(*) + :global(*) {
    margin-left: -4px;
  }

  .constellation-presence-stack-caption {
    color: var(--constellation-label-meta);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
</style>
