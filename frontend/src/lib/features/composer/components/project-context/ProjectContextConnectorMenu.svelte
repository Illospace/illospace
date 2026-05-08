<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import type { ConnectorMode } from './projectContextProfiles';

  let {
    onOpen,
  }: {
    onOpen?: (mode: ConnectorMode) => void;
  } = $props();
</script>

<div class="project-connector-grid">
  <button type="button" class="project-connector-card" onclick={() => onOpen?.('github')}>
    <ConstellationIcon name="git-branch" size={16} stroke={2} />
    <span class="project-connector-card-copy"><strong>GitHub repo</strong><small>Add a repository as project context</small></span>
    <ConstellationIcon name="chevron-right" size={15} stroke={1.9} className="project-connector-card-caret" />
  </button>
  <button type="button" class="project-connector-card" onclick={() => onOpen?.('local')}>
    <ConstellationIcon name="folder" size={16} stroke={2} />
    <span class="project-connector-card-copy"><strong>Files or folders</strong><small>Upload standalone files or a folder snapshot</small></span>
    <ConstellationIcon name="chevron-right" size={15} stroke={1.9} className="project-connector-card-caret" />
  </button>
</div>
