<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';

  let {
    editorContent = $bindable(''),
    fileSaveLoading = false,
    fileSaveError = '',
    fileSaveNotice = '',
    onCancel,
    onSave,
  }: {
    editorContent: string;
    fileSaveLoading?: boolean;
    fileSaveError?: string;
    fileSaveNotice?: string;
    onCancel: () => void;
    onSave: () => void | Promise<void>;
  } = $props();
</script>

<div class="project-file-editor">
  <div class="project-preview-layer-head">
    <strong>Edit thread draft</strong>
    <span>Saved changes stay in the draft until publish.</span>
  </div>
  <textarea
    bind:value={editorContent}
    spellcheck="false"
    aria-label="Thread draft file contents"
  ></textarea>
  {#if fileSaveError}
    <div class="project-file-save-message" data-tone="warning">{fileSaveError}</div>
  {:else if fileSaveNotice}
    <div class="project-file-save-message" data-tone="clean">{fileSaveNotice}</div>
  {/if}
  <div class="project-file-editor-actions">
    <button type="button" onclick={onCancel} disabled={fileSaveLoading}>Cancel</button>
    <button type="button" data-primary="true" onclick={onSave} disabled={fileSaveLoading}>
      <ConstellationIcon name="check" size={13} />
      <span>{fileSaveLoading ? 'Saving...' : 'Save draft file'}</span>
    </button>
  </div>
</div>

<style>
  .project-file-editor {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .project-preview-layer-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
  }

  .project-preview-layer-head strong {
    color: rgba(243, 247, 255, 0.86);
    font-size: 11px;
  }

  .project-preview-layer-head span {
    color: rgba(231, 238, 247, 0.52);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.35;
  }

  .project-file-editor textarea {
    width: 100%;
    min-height: 280px;
    resize: vertical;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 7px;
    padding: 10px;
    background: rgba(6, 10, 15, 0.34);
    color: rgba(240, 245, 251, 0.86);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 11px;
    line-height: 1.55;
    outline: none;
    white-space: pre-wrap;
  }

  .project-file-editor-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 6px;
  }

  .project-file-editor-actions button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    min-height: 26px;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 7px;
    padding: 4px 8px;
    background: rgba(255, 255, 255, 0.045);
    color: rgba(239, 244, 251, 0.76);
    font-size: 10px;
    line-height: 1.1;
    cursor: pointer;
  }

  .project-file-editor-actions button:disabled {
    cursor: default;
    opacity: 0.48;
  }

  .project-file-save-message {
    border-radius: 7px;
    padding: 7px 8px;
    font-size: 11px;
    line-height: 1.35;
  }

  .project-file-save-message[data-tone='clean'] {
    background: color-mix(in srgb, var(--positive, #6BC785) 12%, transparent);
    color: color-mix(in srgb, var(--positive, #6BC785) 78%, white);
  }

  .project-file-save-message[data-tone='warning'] {
    background: rgba(236, 180, 95, 0.11);
    color: #e7bc77;
  }
</style>
