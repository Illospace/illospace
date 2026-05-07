<script lang="ts">
  import { splitSkillMentions } from '$lib/utils/skillMention';

  let {
    value = '',
    scrollTop = 0,
  }: {
    value?: string;
    scrollTop?: number;
  } = $props();

  const segments = $derived(splitSkillMentions(value));
  const contentStyle = $derived(`transform: translateY(-${scrollTop}px);`);
</script>

<div class="skill-mention-overlay" aria-hidden="true">
  <div class="skill-mention-content" style={contentStyle}>{#each segments as segment}{#if segment.kind === 'skill'}<span class="skill-mention-token"><span class="skill-mention-sigil">/</span>{segment.name}</span>{:else}{segment.text}{/if}{/each}</div>
</div>

<style>
  .skill-mention-overlay {
    position: absolute;
    inset: 0;
    pointer-events: none;
    overflow: hidden;
    color: var(--constellation-composer-textarea);
    font: inherit;
    font-size: var(--skill-mention-font-size, inherit);
    line-height: var(--skill-mention-line-height, inherit);
    letter-spacing: 0;
    white-space: pre-wrap;
    overflow-wrap: break-word;
    box-sizing: border-box;
    padding: var(--skill-mention-padding, 0);
  }

  .skill-mention-content {
    min-height: 100%;
  }

  .skill-mention-token {
    color: var(--constellation-skill-mention-text, rgba(231, 224, 255, 0.98));
    background: var(--constellation-skill-mention-background, rgba(155, 128, 255, 0.15));
    border-radius: 5px;
    box-shadow: 0 0 0 1px var(--constellation-skill-mention-border, rgba(194, 176, 255, 0.18));
    text-shadow: none;
    font-weight: inherit;
    font-variant-ligatures: inherit;
    font-feature-settings: inherit;
  }

  .skill-mention-sigil {
    color: var(--constellation-skill-mention-sigil, rgba(181, 160, 255, 0.98));
    font-weight: inherit;
  }
</style>
