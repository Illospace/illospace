<script lang="ts">
  import ConstellationPresenceSeed from './ConstellationPresenceSeed.svelte';
  import type { ConstellationTone } from './constellationTypes';

  export type ConstellationActivityFeedItem = {
    name: string;
    tone: ConstellationTone;
    text: string;
    at: string;
    seedStyle?: string;
    actorColor?: string;
  };

  type Props = {
    items: ConstellationActivityFeedItem[];
    className?: string;
  };

  let { items, className = '' }: Props = $props();

  function actorToneClass(tone: ConstellationTone) {
    return tone === 'amber'
      ? 'constellation-activity-feed-actor-amber'
      : 'constellation-activity-feed-actor-spectral';
  }

  const rootClass = $derived(
    ['constellation-activity-feed', className].filter(Boolean).join(' '),
  );
</script>

<ul class={rootClass}>
  {#each items as item (`${item.name}-${item.at}`)}
    <li class="constellation-activity-feed-item">
      <div class="constellation-activity-feed-lead">
        <ConstellationPresenceSeed
          label={item.name}
          tone={item.tone}
          size="xs"
          style={item.seedStyle}
        />
        <p class="constellation-activity-feed-text">
          <span
            class={`constellation-activity-feed-actor ${actorToneClass(item.tone)}`}
            style={item.actorColor ? `color: ${item.actorColor}` : undefined}
          >
            {item.name}
          </span>
          {item.text}
        </p>
      </div>
      <span class="constellation-activity-feed-meta">{item.at}</span>
    </li>
  {/each}
</ul>

<style>
  .constellation-activity-feed {
    display: grid;
    gap: 0;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .constellation-activity-feed-item {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 14px;
    padding: 12px 0;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .constellation-activity-feed-item:first-child {
    padding-top: 0;
    border-top: 0;
  }

  .constellation-activity-feed-item:last-child {
    padding-bottom: 0;
  }

  .constellation-activity-feed-lead {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    min-width: 0;
  }

  .constellation-activity-feed-text {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.6;
  }

  .constellation-activity-feed-actor {
    font-weight: 600;
    margin-right: 0.35em;
  }

  .constellation-activity-feed-actor-spectral {
    color: var(--constellation-color-spectral-owner);
  }

  .constellation-activity-feed-actor-amber {
    color: var(--constellation-color-amber-owner);
  }

  .constellation-activity-feed-meta {
    flex-shrink: 0;
    color: var(--constellation-color-text-tertiary);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  @media (max-width: 980px) {
    .constellation-activity-feed-item {
      flex-direction: column;
    }

    .constellation-activity-feed-meta {
      padding-left: 28px;
    }
  }
</style>
