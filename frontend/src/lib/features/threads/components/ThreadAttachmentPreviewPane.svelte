<script lang="ts">
  import {
    ConstellationIcon,
    ConstellationIconButton,
  } from '$lib/components/constellation';
  import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';
  import { previewUpload, type UploadPreview } from '$lib/features/threads/api/threadApi';
  import type {
    CortexThreadStageFileAttachment,
    CortexThreadStageImageAttachment,
  } from '$lib/features/threads/domain/threadTranscriptAdapter';
  import { attachmentDownloadUrl, formatAttachmentBytes } from '$lib/utils/attachmentPreview';
  import { renderReadableMarkdown } from '$lib/utils/readableMarkdown';

  type PreviewAttachment = CortexThreadStageImageAttachment | CortexThreadStageFileAttachment;

  let {
    attachment = null,
  }: {
    attachment?: PreviewAttachment | null;
  } = $props();

  let loading = $state(false);
  let error = $state('');
  let preview = $state<UploadPreview | null>(null);
  let requestSeq = 0;

  const attachmentUrl = $derived(attachment?.url ?? '');
  const attachmentLabel = $derived(
    attachment?.kind === 'image' ? attachment.alt : attachment?.label ?? 'Attachment',
  );
  const attachmentDetail = $derived(
    attachment?.kind === 'file' ? attachment.detail ?? '' : '',
  );
  const externalUrl = $derived(
    preview?.download_url || (attachment ? attachmentDownloadUrl(attachment) : '') || attachmentUrl,
  );
  const previewUrl = $derived(preview?.url || attachmentUrl);
  const previewMode = $derived(preview?.preview_mode ?? '');
  const fileKind = $derived(preview?.kind ?? (attachment?.kind === 'image' ? 'image' : attachment?.previewKind ?? 'file'));
  const isMarkdownPreview = $derived(
    previewMode === 'text'
      && (preview?.extension === 'md' || preview?.content_type === 'text/markdown'),
  );
  const fileSize = $derived(
    typeof preview?.size === 'number' ? formatAttachmentBytes(preview.size) : '',
  );
  const metaDetail = $derived(
    [preview?.content_type || attachmentDetail, fileSize].filter(Boolean).join(' | '),
  );

  $effect(() => {
    const url = attachmentUrl;
    const seq = ++requestSeq;
    preview = null;
    error = '';
    loading = false;
    if (!url) return;

    loading = true;
    previewUpload(url)
      .then((result) => {
        if (seq !== requestSeq) return;
        preview = result;
      })
      .catch((err) => {
        if (seq !== requestSeq) return;
        error = err?.detail || 'Preview unavailable';
      })
      .finally(() => {
        if (seq === requestSeq) loading = false;
      });
  });

  function iconName(kind: string | undefined): ConstellationIconName {
    if (kind === 'image') return 'image';
    if (kind === 'video') return 'video';
    if (kind === 'pdf') return 'pdf';
    if (kind === 'html') return 'code';
    if (kind === 'archive') return 'archive';
    if (kind === 'text') return 'code';
    if (kind === 'file') return 'file';
    return 'document';
  }

  function openExternal() {
    if (!externalUrl || typeof window === 'undefined') return;
    window.open(externalUrl, '_blank', 'noopener,noreferrer');
  }
</script>

<div class="thread-attachment-preview-pane">
  {#if attachment}
    <header class="preview-pane-header">
      <span class="preview-pane-icon" aria-hidden="true">
        <ConstellationIcon name={iconName(fileKind)} size={18} stroke={1.8} />
      </span>
      <div class="preview-pane-title">
        <strong>{preview?.filename || attachmentLabel}</strong>
        {#if metaDetail}
          <span>{metaDetail}</span>
        {/if}
      </div>
      <ConstellationIconButton
        label="Open attachment"
        title="Open attachment"
        onclick={openExternal}
        disabled={!externalUrl}
      >
        <ConstellationIcon name="external-link" size={14} stroke={1.8} />
      </ConstellationIconButton>
    </header>

    <section class="preview-pane-body" class:is-loading={loading}>
      {#if loading && !preview}
        <div class="preview-pane-empty">Loading preview</div>
      {:else if error}
        <div class="preview-pane-fallback">
          <ConstellationIcon name={iconName(fileKind)} size={34} stroke={1.5} />
          <strong>{attachmentLabel}</strong>
          <span>{error}</span>
          <button type="button" onclick={openExternal} disabled={!externalUrl}>Open attachment</button>
        </div>
      {:else if previewMode === 'embed' && fileKind === 'image'}
        <img class="preview-pane-media" src={previewUrl} alt={attachmentLabel} />
      {:else if previewMode === 'embed' && fileKind === 'video'}
        <!-- svelte-ignore a11y_media_has_caption -->
        <video class="preview-pane-media" src={previewUrl} controls playsinline preload="metadata"></video>
      {:else if previewMode === 'embed'}
        <iframe src={previewUrl} title={attachmentLabel} referrerpolicy="no-referrer"></iframe>
      {:else if previewMode === 'html' && preview?.text}
        <iframe
          srcdoc={preview.text}
          title={attachmentLabel}
          referrerpolicy="no-referrer"
          sandbox="allow-popups"
        ></iframe>
      {:else if previewMode === 'sheet' && preview?.sheets?.length}
        <div class="preview-sheet-stack">
          {#each preview.sheets as sheet (`sheet-${sheet.index}`)}
            <section class="preview-sheet">
              <h3>{sheet.name}</h3>
              <div class="preview-sheet-scroll">
                <table>
                  <tbody>
                    {#each sheet.rows as row, rowIndex (`row-${rowIndex}`)}
                      <tr>
                        {#each row as cell, cellIndex (`cell-${rowIndex}-${cellIndex}`)}
                          <td>{cell}</td>
                        {/each}
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            </section>
          {/each}
        </div>
      {:else if previewMode === 'slides' && preview?.slides?.length}
        <div class="preview-slide-list">
          {#each preview.slides as slide (`slide-${slide.index}`)}
            <section class="preview-slide">
              <h3>{slide.title}</h3>
              <pre>{slide.text}</pre>
            </section>
          {/each}
        </div>
      {:else if isMarkdownPreview && preview?.text}
        <div class="preview-pane-document constellation-prose">
          {@html renderReadableMarkdown(preview.text)}
        </div>
      {:else if preview?.text}
        <pre class="preview-pane-text">{preview.text}</pre>
      {:else}
        <div class="preview-pane-fallback">
          <ConstellationIcon name={iconName(fileKind)} size={34} stroke={1.5} />
          <strong>{attachmentLabel}</strong>
          <span>{metaDetail || 'Preview unavailable'}</span>
          <button type="button" onclick={openExternal} disabled={!externalUrl}>Open attachment</button>
        </div>
      {/if}
    </section>
  {:else}
    <div class="preview-pane-empty">No attachment selected</div>
  {/if}
</div>

<style>
  .thread-attachment-preview-pane {
    width: 100%;
    height: 100%;
    min-height: 0;
    display: flex;
    flex-direction: column;
    color: rgba(246, 248, 253, 0.9);
    background: transparent;
  }

  .preview-pane-header {
    min-height: 48px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  }

  .preview-pane-icon {
    width: 30px;
    height: 30px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.055);
    color: rgba(157, 194, 255, 0.92);
  }

  .preview-pane-title {
    min-width: 0;
    display: grid;
    gap: 3px;
  }

  .preview-pane-title strong,
  .preview-pane-title span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .preview-pane-title strong {
    color: rgba(248, 251, 255, 0.94);
    font-size: 12px;
    line-height: 1.25;
    font-weight: 650;
  }

  .preview-pane-title span {
    color: rgba(240, 244, 252, 0.48);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.3;
  }

  .preview-pane-body {
    flex: 1;
    min-height: 0;
    display: flex;
    overflow: hidden;
    background: rgba(4, 8, 13, 0.66);
  }

  .preview-pane-body iframe,
  .preview-pane-media {
    width: 100%;
    height: 100%;
    border: 0;
    display: block;
    background: rgba(255, 255, 255, 0.96);
  }

  .preview-pane-media {
    object-fit: contain;
    background: rgba(4, 8, 13, 0.9);
  }

  .preview-pane-text {
    width: 100%;
    height: 100%;
    margin: 0;
    padding: 18px;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: rgba(248, 251, 255, 0.88);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 11px;
    line-height: 1.55;
  }

  .preview-pane-document {
    width: 100%;
    height: 100%;
    padding: 20px;
    overflow: auto;
    color: rgba(248, 251, 255, 0.92);
    background: rgba(255, 255, 255, 0.025);
  }

  .preview-sheet-stack,
  .preview-slide-list {
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow: auto;
    padding: 12px;
    display: grid;
    align-content: start;
    gap: 12px;
  }

  .preview-sheet,
  .preview-slide {
    min-width: 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    padding-bottom: 12px;
  }

  .preview-sheet h3,
  .preview-slide h3 {
    margin: 0 0 8px;
    color: rgba(248, 251, 255, 0.88);
    font-size: 12px;
    line-height: 1.3;
    font-weight: 650;
  }

  .preview-sheet-scroll {
    overflow: auto;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 7px;
  }

  .preview-sheet table {
    min-width: 100%;
    border-collapse: collapse;
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.35;
  }

  .preview-sheet td {
    max-width: 180px;
    padding: 6px 8px;
    border-right: 1px solid rgba(255, 255, 255, 0.06);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    color: rgba(248, 251, 255, 0.82);
    overflow-wrap: anywhere;
    vertical-align: top;
  }

  .preview-slide pre {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: rgba(240, 244, 252, 0.72);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 11px;
    line-height: 1.5;
  }

  .preview-pane-empty,
  .preview-pane-fallback {
    width: 100%;
    height: 100%;
    min-height: 0;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 9px;
    padding: 18px;
    text-align: center;
    color: rgba(235, 242, 250, 0.54);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 11px;
    line-height: 1.45;
  }

  .preview-pane-fallback strong {
    max-width: 100%;
    color: rgba(248, 251, 255, 0.88);
    font-family: var(--constellation-font-sans, var(--font-sans));
    font-size: 13px;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }

  .preview-pane-fallback span {
    max-width: 280px;
    overflow-wrap: anywhere;
  }

  .preview-pane-fallback button {
    min-height: 30px;
    padding: 0 12px;
    border-radius: 7px;
    border: 1px solid rgba(255, 255, 255, 0.12);
    background: rgba(255, 255, 255, 0.07);
    color: rgba(248, 251, 255, 0.9);
    font: inherit;
    cursor: pointer;
  }

  .preview-pane-fallback button:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  :global(:root[data-color-scheme='light']) .thread-attachment-preview-pane {
    color: rgba(24, 35, 49, 0.88);
  }

  :global(:root[data-color-scheme='light']) .preview-pane-header {
    border-bottom-color: rgba(24, 35, 49, 0.08);
  }

  :global(:root[data-color-scheme='light']) .preview-pane-icon {
    background: rgba(49, 63, 76, 0.07);
    color: rgba(31, 48, 65, 0.86);
  }

  :global(:root[data-color-scheme='light']) .preview-pane-title strong,
  :global(:root[data-color-scheme='light']) .preview-sheet h3,
  :global(:root[data-color-scheme='light']) .preview-slide h3,
  :global(:root[data-color-scheme='light']) .preview-pane-fallback strong {
    color: rgba(17, 24, 35, 0.92);
  }

  :global(:root[data-color-scheme='light']) .preview-pane-title span,
  :global(:root[data-color-scheme='light']) .preview-pane-empty,
  :global(:root[data-color-scheme='light']) .preview-pane-fallback,
  :global(:root[data-color-scheme='light']) .preview-slide pre {
    color: rgba(78, 91, 108, 0.66);
  }

  :global(:root[data-color-scheme='light']) .preview-pane-body {
    background: rgba(248, 250, 248, 0.74);
  }

  :global(:root[data-color-scheme='light']) .preview-pane-text,
  :global(:root[data-color-scheme='light']) .preview-pane-document,
  :global(:root[data-color-scheme='light']) .preview-sheet td {
    color: rgba(24, 35, 49, 0.84);
  }

  :global(:root[data-color-scheme='light']) .preview-sheet-scroll,
  :global(:root[data-color-scheme='light']) .preview-sheet td,
  :global(:root[data-color-scheme='light']) .preview-sheet,
  :global(:root[data-color-scheme='light']) .preview-slide {
    border-color: rgba(24, 35, 49, 0.08);
  }

  :global(:root[data-color-scheme='light']) .preview-pane-fallback button {
    border-color: rgba(24, 35, 49, 0.12);
    background: rgba(24, 35, 49, 0.06);
    color: rgba(17, 24, 35, 0.9);
  }
</style>
