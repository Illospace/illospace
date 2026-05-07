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
    <span><strong>GitHub repo</strong><small>Add a repository as project context</small></span>
  </button>
  <button type="button" class="project-connector-card" onclick={() => onOpen?.('folder')}>
    <ConstellationIcon name="folder" size={16} stroke={2} />
    <span><strong>Folder tree</strong><small>Upload a folder snapshot and keep relative paths</small></span>
  </button>
  <button type="button" class="project-connector-card" onclick={() => onOpen?.('file')}>
    <ConstellationIcon name="file" size={16} stroke={2} />
    <span><strong>Individual files</strong><small>Upload one or more standalone files</small></span>
  </button>
</div>
