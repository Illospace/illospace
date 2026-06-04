<script lang="ts">
  import ProjectDraftStatePanel from './ProjectDraftStatePanel.svelte';

  type DraftIdea = {
    id?: string | null;
  } | null;

  let {
    idea,
    runId = null,
    filePath = '',
  }: {
    idea: DraftIdea;
    runId?: string | number | null;
    filePath?: string | null;
  } = $props();
</script>

<section class="thread-project-file-preview-pane" aria-label="Project file preview">
  <ProjectDraftStatePanel
    {idea}
    {runId}
    focusedFilePath={filePath}
    previewOnly
  />
</section>

<style>
  .thread-project-file-preview-pane {
    display: flex;
    flex-direction: column;
    min-height: 100%;
    min-width: 0;
    color: rgba(239, 244, 251, 0.86);
  }

  :global(:root[data-color-scheme='light']) .thread-project-file-preview-pane {
    color: rgba(29, 39, 49, 0.86);
  }

</style>
