<script lang="ts">
  import { onDestroy } from 'svelte';
  import DOMPurify from 'dompurify';
  import { safeVisualImageSrc } from '$lib/utils/visualImageSource';

  let { block }: { block: { type: string; content: string; title?: string; language?: string } } = $props();

  let expanded = $state(true);
  let fullscreen = $state(false);
  let showSource = $state(false);
  let copied = $state(false);
  let copyTimer: ReturnType<typeof setTimeout> | undefined;
  let iframeRef: HTMLIFrameElement | undefined = $state();
  let mermaidContainer: HTMLDivElement | undefined = $state();

  // ── Chart helpers ──────────────────────────────────────────
  type ChartData = {
    type?: 'bar' | 'line' | 'pie' | 'scatter';
    data: { label: string; value: number }[];
    title?: string;
    xlabel?: string;
    ylabel?: string;
  };

  function parseChart(content: string): ChartData | null {
    try {
      const parsed = JSON.parse(content);
      if (Array.isArray(parsed)) return { type: 'bar', data: parsed };
      return parsed;
    } catch { return null; }
  }

  function chartMax(data: { value: number }[]): number {
    return Math.max(...data.map(d => d.value), 1);
  }

  // SVG chart renderers
  function barChartPath(data: { label: string; value: number }[], w: number, h: number) {
    const max = chartMax(data);
    const barW = Math.min(40, (w - 60) / data.length - 4);
    const startX = 50;
    return data.map((d, i) => {
      const barH = (d.value / max) * (h - 40);
      const x = startX + i * (barW + 4);
      const y = h - 25 - barH;
      return { x, y, w: barW, h: barH, label: d.label, value: d.value };
    });
  }

  function lineChartPoints(data: { label: string; value: number }[], w: number, h: number): string {
    const max = chartMax(data);
    const startX = 50;
    const usableW = w - 70;
    const step = data.length > 1 ? usableW / (data.length - 1) : 0;
    return data.map((d, i) => {
      const x = startX + i * step;
      const y = h - 25 - (d.value / max) * (h - 40);
      return `${x},${y}`;
    }).join(' ');
  }

  function pieSlices(data: { label: string; value: number }[]) {
    const total = data.reduce((s, d) => s + d.value, 0) || 1;
    const colors = ['var(--thread-accent, #57CFA0)', '#4BACB8', '#B85C4B', '#7B4BE8', '#4BE87B', '#E84B8A', '#8AE84B', '#4B7BE8'];
    let cumAngle = -Math.PI / 2;
    return data.map((d, i) => {
      const angle = (d.value / total) * Math.PI * 2;
      const startAngle = cumAngle;
      cumAngle += angle;
      const endAngle = cumAngle;
      const largeArc = angle > Math.PI ? 1 : 0;
      const cx = 100, cy = 100, r = 80;
      const x1 = cx + r * Math.cos(startAngle);
      const y1 = cy + r * Math.sin(startAngle);
      const x2 = cx + r * Math.cos(endAngle);
      const y2 = cy + r * Math.sin(endAngle);
      const path = `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} Z`;
      return { path, color: colors[i % colors.length], label: d.label, value: d.value, pct: ((d.value / total) * 100).toFixed(1) };
    });
  }

  // ── Diff helpers ───────────────────────────────────────────
  function renderDiffLines(content: string) {
    let lineNum = { old: 0, new: 0 };
    return content.split('\n').map((line) => {
      let oldNum = '', newNum = '';
      if (line.startsWith('@@')) {
        const match = line.match(/@@ -(\d+)/);
        if (match) { lineNum.old = parseInt(match[1]) - 1; lineNum.new = parseInt(match[1]) - 1; }
        return { text: line, cls: 'diff-hunk', oldNum: '', newNum: '' };
      }
      if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')) {
        return { text: line, cls: 'diff-meta', oldNum: '', newNum: '' };
      }
      if (line.startsWith('+')) {
        lineNum.new++;
        return { text: line, cls: 'diff-add', oldNum: '', newNum: String(lineNum.new) };
      }
      if (line.startsWith('-')) {
        lineNum.old++;
        return { text: line, cls: 'diff-del', oldNum: String(lineNum.old), newNum: '' };
      }
      lineNum.old++; lineNum.new++;
      return { text: line, cls: '', oldNum: String(lineNum.old), newNum: String(lineNum.new) };
    });
  }

  // ── Mermaid detection ──────────────────────────────────────
  function isMermaid(content: string): boolean {
    const trimmed = content.trim();
    return /^(graph |flowchart |sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie |gitGraph|journey|mindmap)/.test(trimmed);
  }

  // ── Markdown renderer (lightweight) ────────────────────────
  function safeHref(url: string): string {
    const trimmed = url.trim();
    if (/^https?:\/\//i.test(trimmed) || /^mailto:/i.test(trimmed)) return trimmed;
    return '';
  }

  function renderMarkdown(md: string): string {
    // 1. Extract code blocks to avoid double-escaping
    const codeBlocks: string[] = [];
    let processed = md.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, lang, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push(`<pre class="md-code-block"><code class="lang-${lang}">${escapeHtml(code.trim())}</code></pre>`);
      return `\x00CB${idx}\x00`;
    });

    const inlineCodes: string[] = [];
    processed = processed.replace(/`([^`]+)`/g, (_m, code) => {
      const idx = inlineCodes.length;
      inlineCodes.push(`<code class="md-inline-code">${escapeHtml(code)}</code>`);
      return `\x00IC${idx}\x00`;
    });

    // 2. Escape HTML in the remaining text
    processed = escapeHtml(processed);

    // 3. Apply markdown patterns
    let html = processed
      // Headers
      .replace(/^#### (.+)$/gm, '<h4>$1</h4>')
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      // Bold / italic
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      // Links (with protocol validation)
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, text, url) => {
        const safe = safeHref(url);
        return safe ? `<a href="${safe}" target="_blank" rel="noopener">${text}</a>` : text;
      })
      // Horizontal rule
      .replace(/^---$/gm, '<hr/>')
      // Unordered lists
      .replace(/^[\-\*] (.+)$/gm, '<li>$1</li>')
      // Ordered lists
      .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
      // Blockquotes
      .replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>')
      // Line breaks → paragraphs
      .replace(/\n\n/g, '</p><p>')
      .replace(/\n/g, '<br/>');
    // Wrap consecutive <li> in <ul>
    html = html.replace(/((?:<li>.*?<\/li>(?:<br\/>)?)+)/g, '<ul>$1</ul>');

    // 4. Restore code blocks
    html = html.replace(/\x00CB(\d+)\x00/g, (_m, idx) => codeBlocks[parseInt(idx)]);
    html = html.replace(/\x00IC(\d+)\x00/g, (_m, idx) => inlineCodes[parseInt(idx)]);

    return `<p>${html}</p>`;
  }

  function escapeHtml(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ── Syntax highlighting (basic) ────────────────────────────
  function highlightCode(code: string, lang?: string): string {
    let escaped = escapeHtml(code);
    // Keywords
    const keywords = /\b(function|const|let|var|return|if|else|for|while|class|import|export|from|def|async|await|try|catch|finally|throw|new|this|self|yield|with|as|in|of|switch|case|break|continue|default|do|typeof|instanceof|void|null|undefined|None|True|False|true|false)\b/g;
    escaped = escaped.replace(keywords, '<span class="hl-kw">$1</span>');
    // Strings
    escaped = escaped.replace(/(["'`])(?:(?!\1|\\).|\\.)*?\1/g, '<span class="hl-str">$&</span>');
    // Comments (single-line)
    escaped = escaped.replace(/(\/\/.*$|#.*$)/gm, '<span class="hl-cmt">$1</span>');
    // Numbers
    escaped = escaped.replace(/\b(\d+\.?\d*)\b/g, '<span class="hl-num">$1</span>');
    return escaped;
  }

  // ── Copy to clipboard ─────────────────────────────────────
  async function copyContent() {
    try {
      await navigator.clipboard.writeText(block.content);
      copied = true;
      copyTimer = setTimeout(() => (copied = false), 2000);
    } catch {}
  }

  onDestroy(() => { if (copyTimer) clearTimeout(copyTimer); });

  // ── iframe setup ──────────────────────────────────────────
  function resizeIframeToContent(iframe: HTMLIFrameElement) {
    try {
      const doc = iframe.contentDocument;
      if (doc?.body) {
        const h = doc.documentElement.scrollHeight || doc.body.scrollHeight;
        iframe.style.height = Math.max(120, h) + 'px';
      }
    } catch {}
  }

  function refreshIframe() {
    if (iframeRef && block.content) {
      const blob = new Blob([block.content], { type: 'text/html' });
      iframeRef.src = URL.createObjectURL(blob);
      iframeRef.onload = () => resizeIframeToContent(iframeRef!);
    }
  }

  $effect(() => {
    if ((block.type === 'preview' || block.type === 'html') && iframeRef && block.content) {
      refreshIframe();
    }
  });

  // ── Mermaid rendering ─────────────────────────────────────
  $effect(() => {
    if (block.type === 'diagram' && isMermaid(block.content) && mermaidContainer) {
      loadMermaid();
    }
  });

  async function loadMermaid() {
    if (!mermaidContainer) return;
    try {
      const { default: mermaid } = await import('mermaid');
      mermaid.initialize({ startOnLoad: false, theme: 'dark', themeVariables: { primaryColor: '#57CFA0', primaryTextColor: '#e0dcd4', lineColor: '#555' } });
      const { svg } = await mermaid.render('mermaid-' + Math.random().toString(36).slice(2), block.content);
      if (mermaidContainer) mermaidContainer.innerHTML = DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true } });
    } catch (e) {
      if (mermaidContainer) mermaidContainer.innerHTML = `<pre style="color:var(--negative)">${escapeHtml(String(e))}</pre>`;
    }
  }

</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="visual-block" class:fullscreen>
  {#if fullscreen}
    <!-- Fullscreen overlay backdrop -->
    <div class="fs-backdrop" onclick={() => (fullscreen = false)}></div>
  {/if}

  <div class="visual-inner" class:fullscreen>
    <div class="visual-header">
      <button class="header-toggle" onclick={() => (expanded = !expanded)}>
        <span class="visual-type">{block.type}</span>
        <span class="visual-title">{block.title || block.type}</span>
        <span class="visual-toggle" class:visual-toggle-open={expanded}>▾</span>
      </button>
      <div class="header-actions">
        {#if block.type !== 'chart'}
          <button class="action-btn" title={copied ? 'Copied!' : 'Copy'} onclick={copyContent}>
            {copied ? '✓' : '⎘'}
          </button>
        {/if}
        {#if (block.type === 'preview' || block.type === 'html') && expanded}
          <button class="action-btn" title="View source" onclick={() => (showSource = !showSource)}>
            {showSource ? '👁' : '</>'}
          </button>
          <button class="action-btn" title="Refresh" onclick={refreshIframe}>↻</button>
        {/if}
        <button class="action-btn" title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'} onclick={() => (fullscreen = !fullscreen)}>
          {fullscreen ? '⊗' : '⛶'}
        </button>
      </div>
    </div>

    {#if expanded}
      <div class="visual-content">

        <!-- ═══ DIFF ═══ -->
        {#if block.type === 'diff'}
          <div class="diff-view">
            {#each renderDiffLines(block.content) as line}
              <div class="diff-line {line.cls}">
                <span class="diff-num old">{line.oldNum}</span>
                <span class="diff-num new">{line.newNum}</span>
                <span class="diff-text">{line.text}</span>
              </div>
            {/each}
          </div>

        <!-- ═══ CHART ═══ -->
        {:else if block.type === 'chart'}
          {@const chart = parseChart(block.content)}
          {#if chart}
            <div class="chart-view">
              {#if chart.title}
                <div class="chart-title">{chart.title}</div>
              {/if}

              {#if !chart.type || chart.type === 'bar'}
                <svg class="chart-svg" viewBox="0 0 400 220" preserveAspectRatio="xMidYMid meet">
                  <!-- Y axis -->
                  <line x1="48" y1="10" x2="48" y2="195" stroke="var(--text-3)" stroke-width="1" opacity="0.3"/>
                  <!-- X axis -->
                  <line x1="48" y1="195" x2="390" y2="195" stroke="var(--text-3)" stroke-width="1" opacity="0.3"/>
                  {#each barChartPath(chart.data, 400, 220) as bar, i}
                    <rect x={bar.x} y={bar.y} width={bar.w} height={bar.h} fill="var(--thread-accent, #57CFA0)" rx="2" opacity="0.85">
                      <title>{bar.label}: {bar.value}</title>
                    </rect>
                    <text x={bar.x + bar.w / 2} y="210" text-anchor="middle" fill="var(--text-3)" font-size="9">{bar.label}</text>
                  {/each}
                  {#if chart.ylabel}
                    <text x="12" y="110" text-anchor="middle" fill="var(--text-3)" font-size="9" transform="rotate(-90, 12, 110)">{chart.ylabel}</text>
                  {/if}
                  {#if chart.xlabel}
                    <text x="220" y="220" text-anchor="middle" fill="var(--text-3)" font-size="9">{chart.xlabel}</text>
                  {/if}
                </svg>

              {:else if chart.type === 'line'}
                <svg class="chart-svg" viewBox="0 0 400 220" preserveAspectRatio="xMidYMid meet">
                  <line x1="48" y1="10" x2="48" y2="195" stroke="var(--text-3)" stroke-width="1" opacity="0.3"/>
                  <line x1="48" y1="195" x2="390" y2="195" stroke="var(--text-3)" stroke-width="1" opacity="0.3"/>
                  <polyline points={lineChartPoints(chart.data, 400, 220)} fill="none" stroke="var(--thread-accent, #57CFA0)" stroke-width="2" stroke-linejoin="round"/>
                  {#each chart.data as d, i}
                    {@const max = chartMax(chart.data)}
                    {@const x = 50 + (chart.data.length > 1 ? i * (330 / (chart.data.length - 1)) : 0)}
                    {@const y = 195 - (d.value / max) * 175}
                    <circle cx={x} cy={y} r="3" fill="var(--thread-accent, #57CFA0)">
                      <title>{d.label}: {d.value}</title>
                    </circle>
                    <text x={x} y="210" text-anchor="middle" fill="var(--text-3)" font-size="9">{d.label}</text>
                  {/each}
                </svg>

              {:else if chart.type === 'pie'}
                <div class="pie-container">
                  <svg class="chart-svg pie-svg" viewBox="0 0 200 200" preserveAspectRatio="xMidYMid meet">
                    {#each pieSlices(chart.data) as slice}
                      <path d={slice.path} fill={slice.color} opacity="0.85" stroke="var(--bg-1)" stroke-width="1">
                        <title>{slice.label}: {slice.value} ({slice.pct}%)</title>
                      </path>
                    {/each}
                  </svg>
                  <div class="pie-legend">
                    {#each pieSlices(chart.data) as slice}
                      <div class="legend-item">
                        <span class="legend-dot" style="background:{slice.color}"></span>
                        <span class="legend-label">{slice.label}</span>
                        <span class="legend-value">{slice.pct}%</span>
                      </div>
                    {/each}
                  </div>
                </div>

              {:else if chart.type === 'scatter'}
                <svg class="chart-svg" viewBox="0 0 400 220" preserveAspectRatio="xMidYMid meet">
                  <line x1="48" y1="10" x2="48" y2="195" stroke="var(--text-3)" stroke-width="1" opacity="0.3"/>
                  <line x1="48" y1="195" x2="390" y2="195" stroke="var(--text-3)" stroke-width="1" opacity="0.3"/>
                  {#each chart.data as d, i}
                    {@const max = chartMax(chart.data)}
                    {@const x = 50 + (chart.data.length > 1 ? i * (330 / (chart.data.length - 1)) : 0)}
                    {@const y = 195 - (d.value / max) * 175}
                    <circle cx={x} cy={y} r="4" fill="var(--thread-accent, #57CFA0)" opacity="0.8">
                      <title>{d.label}: {d.value}</title>
                    </circle>
                  {/each}
                </svg>
              {/if}
            </div>
          {:else}
            <pre class="code-view"><code>{block.content}</code></pre>
          {/if}

        <!-- ═══ PREVIEW / HTML ═══ -->
        {:else if block.type === 'preview' || block.type === 'html'}
          {#if showSource}
            <div class="source-view">
              <pre class="code-view"><code>{block.content}</code></pre>
            </div>
          {:else}
            <iframe
              bind:this={iframeRef}
              class="preview-frame"
              sandbox="allow-scripts allow-same-origin"
              title={block.title || 'Preview'}
            ></iframe>
          {/if}

        <!-- ═══ DIAGRAM ═══ -->
        {:else if block.type === 'diagram'}
          {#if isMermaid(block.content)}
            <div class="diagram-view" bind:this={mermaidContainer}>
              <div class="diagram-loading">Rendering diagram…</div>
            </div>
          {:else}
            <!-- Raw SVG -->
            <div class="diagram-view">
              {@html DOMPurify.sanitize(block.content, { USE_PROFILES: { svg: true } })}
            </div>
          {/if}

        <!-- ═══ MARKDOWN ═══ -->
        {:else if block.type === 'markdown'}
          <div class="markdown-view constellation-prose">
            {@html renderMarkdown(block.content)}
          </div>

        <!-- ═══ IMAGE / SCREENSHOT ═══ -->
        {:else if block.type === 'image' || block.type === 'screenshot'}
          {@const imageSrc = safeVisualImageSrc(block.content)}
          {#if imageSrc}
            <figure class="screenshot-view">
              <img src={imageSrc} alt={block.title || (block.type === 'image' ? 'Image' : 'Screenshot')} />
            </figure>
          {:else}
            <pre class="code-view"><code>{block.content}</code></pre>
          {/if}

        <!-- ═══ CODE ═══ -->
        {:else if block.type === 'code'}
          <div class="code-block-view">
            {#if block.language}
              <div class="code-lang-badge">{block.language}</div>
            {/if}
            <pre class="code-highlighted"><code>{@html highlightCode(block.content, block.language)}</code></pre>
          </div>

        <!-- ═══ FALLBACK ═══ -->
        {:else}
          <pre class="code-view"><code>{block.content}</code></pre>
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .visual-block {
    position: relative;
    width: min(100%, 760px);
  }
  .visual-block.fullscreen {
    position: fixed;
    inset: 0;
    z-index: 9999;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .fs-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.8);
    backdrop-filter: blur(4px);
    z-index: -1;
  }

  .visual-inner {
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 16px;
    overflow: hidden;
    background: rgba(255, 255, 255, 0.035);
    width: 100%;
  }
  .visual-inner.fullscreen {
    width: 90vw;
    max-width: 1200px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 8px 40px rgba(0,0,0,0.5);
  }

  /* ── Header ── */
  .visual-header {
    display: flex;
    align-items: center;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  }

  .header-toggle {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
    padding: 10px 12px;
    background: none;
    border: none;
    cursor: pointer;
    color: rgba(240, 240, 250, 0.76);
  }
  .header-toggle:hover { color: rgba(255, 255, 255, 0.9); }

  .visual-type,
  .visual-toggle {
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .visual-type { color: rgba(141, 183, 255, 0.94); }

  .visual-title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    color: rgba(255, 255, 255, 0.94);
    font-family: var(--constellation-font-sans, var(--font-sans));
    font-size: 13px;
    font-weight: 550;
    text-align: left;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .visual-toggle {
    color: rgba(240, 240, 250, 0.42);
    transition: transform 180ms ease;
  }

  .visual-toggle-open {
    transform: rotate(180deg);
  }

  .header-actions {
    display: flex;
    gap: 4px;
    padding-right: 8px;
  }

  .action-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 28px;
    height: 28px;
    padding: 0 8px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.03);
    color: rgba(240, 240, 250, 0.58);
    cursor: pointer;
    font-size: 10px;
    font-family: var(--constellation-font-mono, var(--font-mono));
    line-height: 1;
  }
  .action-btn:hover {
    color: rgba(255, 255, 255, 0.86);
    border-color: rgba(255, 255, 255, 0.12);
    background: rgba(255, 255, 255, 0.05);
  }

  /* ── Content area ── */
  .visual-content {
    padding: 0;
    overflow: auto;
  }
  .fullscreen .visual-content {
    flex: 1;
    overflow: auto;
  }

  /* ── Diff ── */
  .diff-view {
    display: grid;
    max-height: 280px;
    overflow: auto;
  }
  .diff-line {
    display: grid;
    grid-template-columns: 38px 38px minmax(0, 1fr);
    gap: 10px;
    white-space: pre;
    padding: 6px 12px;
    color: rgba(240, 240, 250, 0.76);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.5;
  }
  .diff-line:hover { background: rgba(255,255,255,0.03); }
  .diff-num {
    color: rgba(240, 240, 250, 0.34);
    user-select: none;
    font-size: 10px;
  }
  .diff-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .diff-add { background: rgba(99, 208, 142, 0.08); }
  .diff-del { background: rgba(225, 121, 121, 0.08); }
  .diff-hunk { background: color-mix(in srgb, var(--thread-accent, #57CFA0) 8%, transparent); }
  .diff-meta { background: rgba(141, 183, 255, 0.08); }

  /* ── Charts ── */
  .chart-view {
    display: grid;
    gap: 10px;
    padding: 14px 14px 16px;
  }
  .chart-title {
    margin: 0;
    color: rgba(255, 255, 255, 0.94);
    font-family: var(--constellation-font-sans, var(--font-sans));
    font-size: 13px;
    font-weight: 550;
  }
  .chart-svg {
    width: 100%;
    max-height: 240px;
  }
  .chart-svg text { font-family: var(--font-sans); }

  .pie-container { display: flex; align-items: center; gap: 16px; justify-content: center; }
  .pie-svg { width: 160px; height: 160px; flex-shrink: 0; }
  .pie-legend { display: flex; flex-direction: column; gap: 4px; }
  .legend-item { display: flex; align-items: center; gap: 6px; font-size: 11px; }
  .legend-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { color: var(--text-2); }
  .legend-value { color: var(--text-3); font-family: var(--font-mono); font-size: 10px; }

  /* ── Preview iframe ── */
  .preview-frame {
    width: 100%;
    min-height: 120px;
    border: none;
    background: white;
    border-radius: 0 0 7px 7px;
  }
  .fullscreen .preview-frame { height: 100%; }

  .source-view { max-height: 300px; overflow: auto; }
  .fullscreen .source-view { max-height: none; flex: 1; }

  /* ── Diagram ── */
  .diagram-view {
    padding: 12px;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 80px;
  }
  .diagram-view :global(svg) { max-width: 100%; height: auto; }
  .diagram-loading { color: var(--text-3); font-size: 11px; font-style: italic; }

  /* ── Markdown ── */
  .markdown-view {
    padding: 12px 16px;
    --constellation-prose-text: rgba(240, 240, 250, 0.82);
    --constellation-prose-heading: rgba(255, 255, 255, 0.94);
    --constellation-prose-muted: rgba(240, 240, 250, 0.72);
    --constellation-prose-accent: var(--thread-accent, #57CFA0);
    --constellation-prose-font-size: 13px;
    --constellation-prose-line-height: 1.65;
  }

  /* ── Screenshot ── */
  .screenshot-view {
    margin: 0;
    padding: 10px;
  }

  .screenshot-view img {
    display: block;
    width: 100%;
    max-height: min(560px, 70vh);
    object-fit: contain;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.04);
  }

  /* ── Code block ── */
  .code-block-view { position: relative; }
  .code-lang-badge {
    position: absolute;
    top: 6px;
    right: 8px;
    font-size: 9px;
    font-family: var(--font-mono);
    color: var(--text-3);
    background: var(--bg-3);
    padding: 1px 6px;
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .code-highlighted {
    margin: 0;
    padding: 10px 12px;
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 11px;
    line-height: 1.6;
    color: rgba(240, 240, 250, 0.82);
    white-space: pre-wrap;
    word-break: break-word;
    overflow-x: auto;
  }
  .code-highlighted :global(.hl-kw) { color: #c586c0; }
  .code-highlighted :global(.hl-str) { color: #ce9178; }
  .code-highlighted :global(.hl-cmt) { color: #6a9955; font-style: italic; }
  .code-highlighted :global(.hl-num) { color: #b5cea8; }

  /* ── Fallback code ── */
  .code-view {
    margin: 0;
    padding: 12px;
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 11px;
    color: rgba(240, 240, 250, 0.82);
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
