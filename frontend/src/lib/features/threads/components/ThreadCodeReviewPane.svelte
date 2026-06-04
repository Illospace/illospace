<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import type { CodeReviewFile } from '$lib/utils/cortexRunPresentation';

  let {
    files = [],
    latestRunStatus = '',
    onPreviewFile,
  }: {
    files?: CodeReviewFile[];
    latestRunStatus?: string | null;
    onPreviewFile?: (file: CodeReviewFile) => void;
  } = $props();

  const changedCount = $derived(files.length);

  function operationLabel(file: CodeReviewFile): string {
    const operation = String(file.operation || file.status || '').trim();
    return operation || 'changed';
  }

  function sourceLabel(file: CodeReviewFile): string {
    if (file.source === 'artifact') return 'artifact';
    return file.tool || 'tool call';
  }

  function previewFile(file: CodeReviewFile) {
    onPreviewFile?.(file);
  }
</script>

<div class="code-review-pane">
  <header class="code-review-header">
    <div class="code-review-title-row">
      <span class="code-review-icon" aria-hidden="true">
        <ConstellationIcon name="code" size={16} stroke={1.8} />
      </span>
      <div>
        <div class="code-review-title">Review files</div>
        <div class="code-review-subtitle">
          {#if changedCount}
            {changedCount} {changedCount === 1 ? 'file' : 'files'} changed by this thread
          {:else if latestRunStatus}
            No changed files detected · latest run {latestRunStatus}
          {:else}
            No code changes detected yet
          {/if}
        </div>
      </div>
    </div>
  </header>

  {#if changedCount}
    <div class="code-review-list" role="list" aria-label="Files to review">
      {#each files as file, index (file.path + '-' + index)}
        <div role="listitem">
          <button
            type="button"
            class="code-review-file"
            disabled={!onPreviewFile}
            title={`Open ${file.path} preview`}
            onclick={() => previewFile(file)}
          >
            <div class="code-review-file-main">
              <span class="code-review-file-icon" aria-hidden="true">
                <ConstellationIcon name="file" size={15} stroke={1.8} />
              </span>
              <code title={file.path}>{file.path}</code>
              <span class="code-review-file-open" aria-hidden="true">
                <ConstellationIcon name="chevron-right" size={14} stroke={1.9} />
              </span>
            </div>
            <div class="code-review-file-meta">
              <span>{operationLabel(file)}</span>
              <span>{sourceLabel(file)}</span>
              {#if file.status}
                <span>{file.status}</span>
              {/if}
            </div>
          </button>
        </div>
      {/each}
    </div>
  {:else}
    <div class="code-review-empty">
      <div class="code-review-empty-orb">
        <ConstellationIcon name="file" size={22} stroke={1.6} />
      </div>
      <strong>No files to review yet</strong>
      <p>When Illo writes or edits files, this panel opens and keeps the changed file list in one column for review.</p>
    </div>
  {/if}
</div>

<style>
  .code-review-pane {
    height: 100%;
    min-height: 0;
    display: flex;
    flex-direction: column;
    color: var(--text-1, #f4f6fa);
  }

  .code-review-header {
    flex: 0 0 auto;
    padding: 14px 16px 12px;
    border-bottom: 1px solid var(--constellation-utility-panel-header-border, rgba(255, 255, 255, 0.09));
  }

  .code-review-title-row {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  .code-review-icon {
    width: 30px;
    height: 30px;
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 12px;
    color: var(--thread-accent, #57cfa0);
    background: color-mix(in srgb, var(--thread-accent, #57cfa0) 14%, transparent);
    border: 1px solid color-mix(in srgb, var(--thread-accent, #57cfa0) 22%, transparent);
  }

  .code-review-title {
    font-size: 14px;
    font-weight: 720;
    letter-spacing: -0.01em;
  }

  .code-review-subtitle {
    margin-top: 2px;
    color: var(--text-3, rgba(244, 246, 250, 0.58));
    font-size: 12px;
    line-height: 1.35;
  }

  .code-review-list {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    display: grid;
    align-content: start;
    gap: 8px;
    padding: 12px;
    scrollbar-color: var(--constellation-utility-panel-scrollbar, rgba(255, 255, 255, 0.18)) transparent;
  }

  .code-review-file {
    width: 100%;
    min-width: 0;
    padding: 11px 12px;
    border-radius: 14px;
    border: 1px solid var(--constellation-card-border, rgba(255, 255, 255, 0.08));
    background: rgba(255, 255, 255, 0.045);
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
    transition:
      border-color 140ms ease,
      background 140ms ease,
      transform 140ms ease;
  }

  .code-review-file:hover,
  .code-review-file:focus-visible {
    border-color: color-mix(in srgb, var(--thread-accent, #57cfa0) 34%, transparent);
    background: color-mix(in srgb, var(--thread-accent, #57cfa0) 9%, rgba(255, 255, 255, 0.055));
  }

  .code-review-file:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--thread-accent, #57cfa0) 42%, transparent);
    outline-offset: 2px;
  }

  .code-review-file:active {
    transform: translateY(1px);
  }

  .code-review-file:disabled {
    cursor: default;
    opacity: 0.72;
  }

  .code-review-file:disabled:hover {
    border-color: var(--constellation-card-border, rgba(255, 255, 255, 0.08));
    background: rgba(255, 255, 255, 0.045);
  }

  .code-review-file-main {
    display: grid;
    grid-template-columns: 18px minmax(0, 1fr) 16px;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .code-review-file-icon {
    color: var(--thread-accent, #57cfa0);
    opacity: 0.9;
  }

  .code-review-file code {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-1, #f4f6fa);
    font-family: var(--constellation-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
    font-size: 12px;
    background: transparent;
  }

  .code-review-file-open {
    color: var(--text-3, rgba(244, 246, 250, 0.54));
    opacity: 0.7;
    transition:
      color 140ms ease,
      opacity 140ms ease,
      transform 140ms ease;
  }

  .code-review-file:hover .code-review-file-open,
  .code-review-file:focus-visible .code-review-file-open {
    color: var(--thread-accent, #57cfa0);
    opacity: 1;
    transform: translateX(2px);
  }

  .code-review-file-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 9px;
    padding-left: 26px;
  }

  .code-review-file-meta span {
    padding: 2px 7px;
    border-radius: 999px;
    color: var(--text-3, rgba(244, 246, 250, 0.62));
    background: rgba(255, 255, 255, 0.055);
    font-size: 10px;
    line-height: 1.5;
    text-transform: lowercase;
  }

  .code-review-empty {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 28px 24px;
    text-align: center;
    color: var(--text-3, rgba(244, 246, 250, 0.58));
  }

  .code-review-empty strong {
    color: var(--text-1, #f4f6fa);
    font-size: 14px;
  }

  .code-review-empty p {
    max-width: 280px;
    margin: 0;
    font-size: 12px;
    line-height: 1.45;
  }

  .code-review-empty-orb {
    width: 48px;
    height: 48px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 18px;
    color: var(--thread-accent, #57cfa0);
    background: color-mix(in srgb, var(--thread-accent, #57cfa0) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--thread-accent, #57cfa0) 18%, transparent);
  }

  :global(:root[data-color-scheme='light']) .code-review-pane {
    color: var(--text-1, #18212a);
  }

  :global(:root[data-color-scheme='light']) .code-review-file {
    background: rgba(49, 63, 76, 0.045);
    border-color: rgba(49, 63, 76, 0.08);
  }

  :global(:root[data-color-scheme='light']) .code-review-file-meta span {
    background: rgba(49, 63, 76, 0.06);
  }
</style>
