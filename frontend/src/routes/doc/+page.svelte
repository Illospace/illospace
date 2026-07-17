<script lang="ts">
  import { page } from '$app/stores';
  import { renderReadableMarkdown } from '$lib/utils/readableMarkdown';
  import {
    docViewerFilename,
    docViewerRenderMode,
    docViewerTitle,
    firstMarkdownHeading,
    normalizeDocViewerSrc,
  } from '$lib/utils/docViewer';

  const src = $derived(normalizeDocViewerSrc($page.url.searchParams.get('src')));
  const titleParam = $derived($page.url.searchParams.get('title'));
  const renderMode = $derived(src ? docViewerRenderMode(src) : 'text');

  let status = $state<'loading' | 'ready' | 'error'>('loading');
  let errorMessage = $state('');
  let documentText = $state('');
  let pdfUrl = $state('');

  const pageTitle = $derived(
    docViewerTitle(titleParam, src, renderMode === 'markdown' ? documentText : ''),
  );
  const markdownHtml = $derived(
    status === 'ready' && renderMode === 'markdown' ? renderReadableMarkdown(documentText) : '',
  );
  const filename = $derived(docViewerFilename(src));
  // Skip the page-level heading when the markdown itself opens with the same title.
  const showTitleHeading = $derived(
    renderMode !== 'markdown'
      || status !== 'ready'
      || firstMarkdownHeading(documentText) !== pageTitle,
  );

  function handleImageError() {
    status = 'error';
    errorMessage = 'Image not found. It may have been removed, or the link is incomplete.';
  }

  $effect(() => {
    const target = src;
    const mode = target ? docViewerRenderMode(target) : 'text';
    let cancelled = false;
    let objectUrl: string | null = null;
    status = 'loading';
    errorMessage = '';
    documentText = '';
    pdfUrl = '';

    if (!target) {
      status = 'error';
      errorMessage =
        'This viewer can only open documents published to /static/uploads on this workspace. '
        + 'Check that the link includes a complete src parameter.';
    } else if (mode === 'image') {
      status = 'ready';
    } else {
      (async () => {
        try {
          const response = await fetch(target);
          if (cancelled) return;
          // The backend serves the SPA index (200, text/html) for missing
          // uploads instead of a plain 404, so treat both as "not found".
          const contentType = (response.headers.get('content-type') || '').toLowerCase();
          if (!response.ok || contentType.includes('text/html')) {
            status = 'error';
            errorMessage = !response.ok && response.status !== 404
              ? `The document could not be loaded (HTTP ${response.status}).`
              : 'Document not found. It may have been removed, or the link is incomplete.';
            return;
          }
          if (mode === 'pdf') {
            const blob = await response.blob();
            if (cancelled) return;
            objectUrl = URL.createObjectURL(
              blob.type === 'application/pdf' ? blob : new Blob([blob], { type: 'application/pdf' }),
            );
            pdfUrl = objectUrl;
          } else {
            const text = await response.text();
            if (cancelled) return;
            documentText = text;
          }
          status = 'ready';
        } catch {
          if (!cancelled) {
            status = 'error';
            errorMessage = 'The document could not be loaded. Check your connection and try again.';
          }
        }
      })();
    }

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  });
</script>

<svelte:head>
  <title>{pageTitle} · Illospace</title>
</svelte:head>

<div class="doc-viewer">
  {#if status === 'loading'}
    <p class="doc-status" role="status">Loading document…</p>
  {:else if status === 'error'}
    <div class="doc-error" role="alert">
      <h1>Can't open this document</h1>
      <p>{errorMessage}</p>
      {#if src && renderMode !== 'image'}
        <p class="doc-error-hint">
          <a href={src} target="_blank" rel="noopener">Try the raw file instead</a>
        </p>
      {/if}
    </div>
  {:else}
    <header class="doc-header">
      {#if showTitleHeading}
        <h1 class="doc-title">{pageTitle}</h1>
      {/if}
      {#if filename}
        <p class="doc-meta">
          <span>{filename}</span>
          {#if renderMode !== 'image'}
            <span aria-hidden="true">·</span>
            <a href={src} target="_blank" rel="noopener">Open raw file</a>
          {/if}
        </p>
      {/if}
    </header>
    {#if renderMode === 'image'}
      <div class="doc-image">
        <img src={src} alt={pageTitle} onerror={handleImageError} />
      </div>
    {:else if renderMode === 'markdown'}
      <!-- renderReadableMarkdown escapes all source HTML; only its own safe markup is injected. -->
      <article class="constellation-prose doc-prose">{@html markdownHtml}</article>
    {:else if renderMode === 'pdf'}
      <div class="doc-pdf">
        <iframe src={pdfUrl} title={pageTitle}></iframe>
      </div>
    {:else}
      <pre class="doc-plain">{documentText}</pre>
    {/if}
  {/if}
  <footer class="doc-footer">Published from an Illospace thread.</footer>
</div>

<style>
  .doc-viewer {
    max-width: 68ch;
    margin: 0 auto;
    padding: clamp(14px, 4vw, 44px) 0 40px;
    min-width: 0;
  }

  .doc-status {
    color: var(--text-3);
    font-size: var(--text-md);
  }

  .doc-error {
    display: grid;
    gap: 10px;
    border: 1px solid var(--warning-border);
    border-radius: var(--radius-md);
    padding: 18px 20px;
    background: var(--warning-surface);
    color: var(--text-2);
    font-size: var(--text-md);
    line-height: var(--leading-normal);
  }

  .doc-error h1 {
    color: var(--text-1);
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
  }

  .doc-error-hint a {
    color: var(--content-link);
  }

  .doc-header {
    display: grid;
    gap: 8px;
    margin-bottom: 26px;
  }

  .doc-header:empty {
    display: none;
  }

  .doc-title {
    color: var(--text-1);
    font-size: var(--text-2xl);
    font-weight: var(--weight-semibold);
    line-height: var(--leading-snug);
    overflow-wrap: anywhere;
  }

  .doc-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
  }

  .doc-meta a {
    color: var(--text-3);
    text-decoration-color: color-mix(in srgb, currentColor 40%, transparent);
  }

  .doc-meta a:hover {
    color: var(--content-link);
  }

  .doc-prose {
    --constellation-prose-font-size: var(--text-lg);
  }

  .doc-image {
    display: grid;
    place-items: center;
    width: 100%;
  }

  .doc-image img {
    display: block;
    max-width: 100%;
    height: auto;
  }

  .doc-pdf iframe {
    display: block;
    width: 100%;
    height: 82vh;
    border: 1px solid var(--border-2);
    border-radius: var(--radius-md);
    background: #fff;
  }

  .doc-plain {
    overflow-x: auto;
    border: 1px solid var(--border-2);
    border-radius: var(--radius-md);
    padding: 14px 16px;
    background: var(--content-code-background);
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--text-sm);
    line-height: var(--leading-relaxed);
    white-space: pre;
  }

  .doc-footer {
    margin-top: 44px;
    border-top: 1px solid var(--border-1);
    padding-top: 14px;
    color: var(--text-3);
    font-size: var(--text-sm);
  }
</style>
