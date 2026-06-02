<script lang="ts">
  import {
    ConstellationButton,
    ConstellationIcon,
    ConstellationIconButton,
    ConstellationPill,
    ConstellationSignalStatusIndicator,
  } from '$lib/components/constellation';
  import ConversationScrollCue from '$lib/components/chat/ConversationScrollCue.svelte';
  import AttachmentPreviewDialog from '$lib/components/chat/AttachmentPreviewDialog.svelte';
  import {
    attachmentDetail,
    attachmentKindLabel,
    attachmentLabel,
    attachmentPreviewKind,
    attachmentUrl,
    normalizeServerUploadPreviewUrl,
  } from '$lib/utils/attachmentPreview';
  import StreamVisualBlock from '$lib/features/threads/components/StreamVisualBlock.svelte';
  import ThreadAuthorMark from '$lib/features/threads/components/ThreadAuthorMark.svelte';
  import ThreadLinkPreviewCard from '$lib/features/threads/components/ThreadLinkPreviewCard.svelte';

  import {
    getCortexThreadRunStatusGlyph,
    getCortexThreadRunStatusLabel,
    getCortexThreadStepStatusGlyph,
    type CortexThreadStageFileAttachment,
    type CortexThreadStageImageAttachment,
    normalizeCortexThreadLiveLine,
    orderCortexThreadRunSteps,
    summarizeCortexThreadRunSteps,
    type CortexThreadStageRunItem,
    type ThreadTranscriptProps,
  } from '$lib/features/threads/domain/threadTranscriptAdapter';
  import {
    RUN_INLINE_SECTIONS,
    attachmentIconName,
    attachmentPreviewLabel,
    attachmentPreviewType,
    getAttachmentKey,
    getMessageTone,
    getMessageClass,
    getRunClass,
    getRunDefaultExpanded,
    getRunKey,
    getRunLiveCueLabel,
    getRunLiveCueWorkIndex,
    getRunSectionKey,
    getStepToneClass,
    getThinkingStatusLabel,
    getThinkingSteps,
    getThreadHeaderStatusLabel,
    getThreadHeaderStatusState,
    getTimelineToolDetail,
    getTimelineToolLabel,
    getTimelineToolTarget,
    getTimelineToolTitle,
    getToolCallDetail,
    getToolCallLabel,
    getUserPresenceStyle,
    getWorkThoughtClass,
    getWorkThoughtHtml,
    hasMessageSupplementalMeta,
    hasVisibleLiveWorkItems,
    isIlloMessage,
    isRunActiveStatus,
    isRunLiveWorkStream,
    shouldRenderLiveWorkItem,
    shouldShowTimelineToolArgs,
    type RunInlineSection,
  } from '$lib/features/threads/domain/threadTranscriptPresentation';

  let {
    header = null,
    transcriptItems = [],
    loading = false,
    loadingLabel = 'Loading thread...',
    emptyLabel = 'No messages yet. Start the conversation.',
    showReplyDock = true,
    showScrollCue = false,
    replyPlaceholder = 'Ask Illo anything...',
    replyHint = 'Illo responds to every note',
    className,
    headerSlot,
    transcriptSlot,
    renderTranscriptItem,
    replyDock,
    onTranscriptScroll,
    onTranscriptReady,
    onScrollToBottom,
    onPreviewAttachment,
  }: ThreadTranscriptProps = $props();

  let transcriptContainerEl: HTMLDivElement | undefined = $state();
  let runExpandedByKey: Record<string, boolean> = $state({});
  let runSectionExpandedByKey: Record<string, boolean> = $state({});
  let runStatusByKey: Record<string, string> = $state({});
  let previewAttachment = $state<CortexThreadStageImageAttachment | CortexThreadStageFileAttachment | null>(null);

  const shellClass = $derived(
    ['thread-transcript', className ?? ''].filter(Boolean).join(' '),
  );
  const hasTranscript = $derived(transcriptItems.length > 0);
  const previewAttachmentUrl = $derived(previewAttachment?.url ?? '');
  const previewAttachmentLabel = $derived(previewAttachment ? attachmentPreviewLabel(previewAttachment) : '');
  const previewAttachmentDetail = $derived(previewAttachment?.kind === 'file' ? (previewAttachment.detail ?? '') : '');
  const previewAttachmentKind = $derived(previewAttachment ? attachmentPreviewType(previewAttachment) : 'file');

  function isRunExpanded(item: CortexThreadStageRunItem, index: number) {
    const key = getRunKey(item, index);
    return runExpandedByKey[key] ?? getRunDefaultExpanded(item);
  }

  function isRunSectionExpanded(
    runKey: string,
    section: RunInlineSection,
    defaultExpanded: boolean | undefined,
  ) {
    return runSectionExpandedByKey[getRunSectionKey(runKey, section)] ?? Boolean(defaultExpanded);
  }

  function handleRunToggle(key: string, event: Event) {
    if (event.target !== event.currentTarget) return;

    const details = event.currentTarget as HTMLDetailsElement | null;
    if (!details) return;
    runExpandedByKey = {
      ...runExpandedByKey,
      [key]: details.open,
    };
  }

  function handleRunSectionToggle(
    runKey: string,
    section: RunInlineSection,
    event: Event,
  ) {
    if (event.target !== event.currentTarget) return;
    event.stopPropagation();

    const details = event.currentTarget as HTMLDetailsElement | null;
    if (!details) return;
    runSectionExpandedByKey = {
      ...runSectionExpandedByKey,
      [getRunSectionKey(runKey, section)]: details.open,
    };
  }

  function openAttachmentPreview(attachment: CortexThreadStageImageAttachment | CortexThreadStageFileAttachment) {
    if (onPreviewAttachment) {
      onPreviewAttachment(attachment);
      return;
    }
    previewAttachment = attachment;
  }

  function closeAttachmentPreview() {
    previewAttachment = null;
  }

  function normalizeServerPreviewUrl(rawHref: string | null | undefined): string {
    const base = typeof window === 'undefined' ? 'http://illo.local' : window.location.origin;
    return normalizeServerUploadPreviewUrl(rawHref, base);
  }

  function previewAttachmentFromLink(anchor: HTMLAnchorElement): CortexThreadStageFileAttachment | null {
    const url = normalizeServerPreviewUrl(anchor.getAttribute('href') || anchor.href);
    if (!url) return null;
    const label = anchor.textContent?.trim() || attachmentLabel({ url });
    const source = { url, filename: label };
    return {
      kind: 'file',
      url: attachmentUrl(source) || url,
      label,
      detail: attachmentKindLabel(source) || attachmentDetail(source),
      previewKind: attachmentPreviewKind(source),
    };
  }

  function handleThreadContentClick(event: MouseEvent) {
    const target = event.target as HTMLElement | null;
    const anchor = target?.closest?.('a');
    if (!(anchor instanceof HTMLAnchorElement)) return;
    const attachment = previewAttachmentFromLink(anchor);
    if (!attachment) return;
    event.preventDefault();
    openAttachmentPreview(attachment);
  }

  function previewServerFileLinks(node: HTMLElement) {
    node.addEventListener('click', handleThreadContentClick);
    return {
      destroy() {
        node.removeEventListener('click', handleThreadContentClick);
      },
    };
  }

  $effect(() => {
    onTranscriptReady?.(transcriptContainerEl);
  });

  $effect(() => {
    const nextState = { ...runExpandedByKey };
    const nextSectionState = { ...runSectionExpandedByKey };
    const nextStatusState = { ...runStatusByKey };
    const activeKeys = new Set<string>();
    const activeSectionKeys = new Set<string>();
    let changed = false;
    let sectionChanged = false;
    let statusChanged = false;

    for (const [index, item] of transcriptItems.entries()) {
      if (item.kind !== 'run') continue;
      const key = getRunKey(item, index);
      activeKeys.add(key);

      const previousStatus = nextStatusState[key];
      if (previousStatus !== item.status) {
        if (previousStatus && isRunActiveStatus(previousStatus) && !isRunActiveStatus(item.status)) {
          nextState[key] = false;
          changed = true;
        }
        nextStatusState[key] = item.status;
        statusChanged = true;
      }

      for (const section of RUN_INLINE_SECTIONS) {
        activeSectionKeys.add(getRunSectionKey(key, section));
      }

    }

    for (const key of Object.keys(nextState)) {
      if (!activeKeys.has(key)) {
        delete nextState[key];
        changed = true;
      }
    }

    for (const key of Object.keys(nextStatusState)) {
      if (!activeKeys.has(key)) {
        delete nextStatusState[key];
        statusChanged = true;
      }
    }

    for (const key of Object.keys(nextSectionState)) {
      if (!activeSectionKeys.has(key)) {
        delete nextSectionState[key];
        sectionChanged = true;
      }
    }

    if (changed) {
      runExpandedByKey = nextState;
    }

    if (sectionChanged) {
      runSectionExpandedByKey = nextSectionState;
    }

    if (statusChanged) {
      runStatusByKey = nextStatusState;
    }
  });
</script>

<section
  class={shellClass}
  data-cortex-surface="thread-shell"
  data-cortex-thread-column="main"
  data-design-composition="ConstellationThreadStageScreen"
  data-design-source="ConstellationThreadStageScreen.ThreadPanel.threadMain"
  aria-label="Thread conversation"
>
  {#if headerSlot}
    {@render headerSlot()}
  {:else if header}
    <header class="thread-panel-header thread-column">
      <div class="thread-header-title-row">
        <ConstellationSignalStatusIndicator
          state={getThreadHeaderStatusState(header)}
          label={getThreadHeaderStatusLabel(header)}
          className="thread-header-status-indicator"
        />
        <h1 class="thread-header-title" title={header.title}>{header.title}</h1>

        {#if header.onLinkAction}
          <ConstellationIconButton
            label={header.linkActionLabel ?? 'Copy thread link'}
            title={header.linkActionLabel ?? 'Copy thread link'}
            className={`thread-header-action-button thread-link-action-button ${header.linkActionLoading ? 'is-loading' : ''}`}
            disabled={header.linkActionLoading}
            onclick={header.onLinkAction}
          >
            <ConstellationIcon name="link" size={13} stroke={1.9} />
          </ConstellationIconButton>
        {/if}

        {#if header.onTitleAction}
          <ConstellationIconButton
            label={header.titleActionLabel ?? 'Generate a new thread title'}
            title={header.titleActionLabel ?? 'Generate a new thread title'}
            className={`thread-header-action-button thread-title-action-button ${header.titleActionLoading ? 'is-loading' : ''}`}
            disabled={header.titleActionLoading}
            onclick={header.onTitleAction}
          >
            <ConstellationIcon name="refresh" size={13} stroke={1.9} />
          </ConstellationIconButton>
        {/if}

        {#if header.onToggleSecondaryPanel || header.onTogglePanel}
          <div class="thread-header-panel-toggle-group">
            {#if header.onToggleSecondaryPanel}
              <ConstellationIconButton
                label={(header.secondaryPanelOpen ? 'Hide ' : 'Show ') + (header.secondaryPanelLabel ?? 'activity').toLowerCase()}
                title={header.secondaryPanelLabel ?? 'Activity'}
                className="thread-header-action-button thread-panel-toggle-button"
                pressed={header.secondaryPanelOpen}
                onclick={header.onToggleSecondaryPanel}
              >
                <ConstellationIcon name="activity" size={14} stroke={1.8} />
              </ConstellationIconButton>
            {/if}

            {#if header.onArchiveAction}
              <ConstellationIconButton
                label={header.archiveActionLabel ?? 'Archive thread'}
                title="Archive"
                className={`thread-header-action-button thread-archive-button ${header.archiveActionLoading ? 'is-loading' : ''}`}
                disabled={header.archiveActionLoading}
                onclick={header.onArchiveAction}
              >
                <ConstellationIcon name="archive-box" size={14} stroke={1.8} />
              </ConstellationIconButton>
            {/if}

            {#if header.onTogglePanel}
              <ConstellationIconButton
                label={(header.panelOpen ? 'Hide ' : 'Show ') + (header.panelLabel ?? 'preview').toLowerCase()}
                title={header.panelLabel ?? 'Preview'}
                className="thread-header-action-button thread-panel-toggle-button"
                pressed={header.panelOpen}
                onclick={header.onTogglePanel}
              >
                <ConstellationIcon name="side-panel" size={14} stroke={1.8} />
              </ConstellationIconButton>
            {/if}
          </div>
        {/if}
      </div>
    </header>
  {/if}

  <div class="thread-content" bind:this={transcriptContainerEl} onscroll={onTranscriptScroll} use:previewServerFileLinks>
    <div class="thread-column message-stack">
      {#if transcriptSlot}
        {@render transcriptSlot()}
      {:else if loading}
        <div class="thread-empty-state thread-loading-state">{loadingLabel}</div>
      {:else if hasTranscript}
        {#each transcriptItems as item, index (`${item.kind}-${item.id ?? index}`)}
          {#if renderTranscriptItem}
            {@render renderTranscriptItem(item)}
          {:else if item.kind === 'message'}
            {@const isIllo = isIlloMessage(item)}
            {@const hasSupplementalMeta = hasMessageSupplementalMeta(item)}
            <article class={getMessageClass(item)}>
              {#if !isIllo}
                <header class="thread-message-header">
                  <ThreadAuthorMark
                    author={item.author}
                    role={item.role}
                    tone={getMessageTone(item)}
                    presenceStyle={getUserPresenceStyle(item)}
                  />

                  <div class="thread-message-meta">
                    <span class="thread-message-author">{item.author}</span>

                    {#if hasSupplementalMeta}
                      <span class="thread-message-meta-supplemental">
                        {#if item.timestamp}
                          <span>{item.timestamp}</span>
                        {/if}

                        {#if item.timestamp && item.tag}
                          <span class="thread-message-meta-divider" aria-hidden="true"></span>
                        {/if}

                        {#if item.tag}
                          <span>{item.tag}</span>
                        {/if}
                      </span>
                    {/if}
                  </div>
                </header>
              {/if}

              <div class="thread-message-content">
                {#if item.html}
                  <div class="thread-message-html constellation-prose">{@html item.html}</div>
                {:else if item.paragraphs}
                  {#each item.paragraphs as paragraph}
                    <p>{paragraph}</p>
                  {/each}
                {/if}

                {#if item.sections}
                  {#each item.sections as section}
                    <section>
                      {#if section.heading}
                        <h2>{section.heading}</h2>
                      {/if}

                      {#if section.paragraphs}
                        {#each section.paragraphs as paragraph}
                          <p>{paragraph}</p>
                        {/each}
                      {/if}

                      {#if section.points && section.ordered}
                        <ol>
                          {#each section.points as point}
                            <li>{point}</li>
                          {/each}
                        </ol>
                      {:else if section.points}
                        <ul>
                          {#each section.points as point}
                            <li>{point}</li>
                          {/each}
                        </ul>
                      {/if}
                    </section>
                  {/each}
                {/if}

                {#if item.attachments && item.attachments.length > 0}
                  <div class="thread-message-attachments">
                    {#each item.attachments as attachment, attachmentIndex (`${getAttachmentKey(attachment, attachmentIndex)}`)}
                      {#if attachment.kind === 'visual'}
                        <section class="thread-visual-surface">
                          <StreamVisualBlock block={attachment.block} />
                        </section>
                      {:else if attachment.kind === 'image'}
                        <button
                          type="button"
                          class="thread-message-image-button"
                          aria-label={`Preview ${attachment.alt}`}
                          onclick={() => openAttachmentPreview(attachment)}
                        >
                          <img class="thread-message-image" src={attachment.url} alt={attachment.alt} />
                        </button>
                      {:else}
                        {@const previewKind = attachmentPreviewType(attachment)}
                        <button
                          type="button"
                          class={`thread-message-file is-${previewKind}`}
                          aria-label={`Preview ${attachment.label}`}
                          onclick={() => openAttachmentPreview(attachment)}
                        >
                          <span class="thread-message-file-icon" aria-hidden="true">
                            <ConstellationIcon name={attachmentIconName(attachment)} size={16} stroke={1.8} />
                          </span>
                          <span class="thread-message-file-copy">
                            <span>{attachment.label}</span>
                            {#if attachment.detail}
                              <small>{attachment.detail}</small>
                            {/if}
                          </span>
                        </button>
                      {/if}
                    {/each}
                  </div>
                {/if}

                {#if item.threadReferences && item.threadReferences.length > 0}
                  <div class="thread-message-thread-previews">
                    {#each item.threadReferences as reference (`${reference.thread_id ?? reference.original_ref ?? reference.url}`)}
                      <ThreadLinkPreviewCard {reference} />
                    {/each}
                  </div>
                {/if}
              </div>
            </article>
          {:else if item.kind === 'visual'}
            <section class="thread-visual-surface">
              <StreamVisualBlock block={item.block} />
            </section>
          {:else if item.kind === 'run'}
            {@const orderedSteps = orderCortexThreadRunSteps(item.runSteps ?? [])}
            {@const stepTone = getStepToneClass(item)}
            {@const runKey = getRunKey(item, index)}
            {@const liveWorkStream = isRunLiveWorkStream(item)}
            {#if liveWorkStream}
              {@const showLiveCue = item.showLiveCue !== false}
              {@const liveCueWorkIndex = getRunLiveCueWorkIndex(item)}
              {@const liveCueLabel = getRunLiveCueLabel(item, liveCueWorkIndex)}
              <section class="run-live-work-stream" aria-label="Live run work">
                {#if item.workItems && item.workItems.length > 0}
                  {#if hasVisibleLiveWorkItems(item, liveCueWorkIndex)}
                    <div class="run-work-timeline" aria-label="Run work timeline">
                      {#each item.workItems as workItem, workIndex (`live-${workItem.kind}-${workItem.at ?? workItem.time ?? 'work'}-${workIndex}`)}
                        {#if shouldRenderLiveWorkItem(workIndex, liveCueWorkIndex)}
                          {#if workItem.kind === 'tool'}
                            <div class={`run-work-item run-work-tool run-work-tool-${workItem.status ?? 'used'}`} title={getTimelineToolTitle(workItem)}>
                              <span class="run-work-tool-icon" aria-hidden="true">
                                {workItem.display?.icon ?? '🔧'}
                              </span>

                              <span class="run-work-tool-copy">
                                <span class="run-work-tool-label">{getTimelineToolLabel(workItem)}</span>
                                {#if getTimelineToolDetail(workItem)}
                                  <span class="run-work-tool-detail">{getTimelineToolDetail(workItem)}</span>
                                {/if}
                                {#if shouldShowTimelineToolArgs(workItem)}
                                  <code class="run-work-tool-args">{workItem.args}</code>
                                {/if}
                              </span>
                            </div>
                          {:else}
                            <div class={getWorkThoughtClass(workItem.text)}>{@html getWorkThoughtHtml(workItem.text)}</div>
                          {/if}
                        {/if}
                      {/each}
                    </div>
                  {/if}
                {:else if item.liveLines && item.liveLines.length > 0}
                  <div class="run-work-timeline" aria-label="Run work timeline">
                    {#each item.liveLines as line, lineIndex (`live-line-${typeof line === 'string' ? line : line.text}-${lineIndex}`)}
                      {@const entry = normalizeCortexThreadLiveLine(line)}
                      <div class={getWorkThoughtClass(entry.text)}>{@html getWorkThoughtHtml(entry.text)}</div>
                    {/each}
                  </div>
                {/if}

                {#if showLiveCue}
                  <div class="run-live-work-cue" aria-live="polite" aria-label={liveCueLabel}>
                    <span class="thinking-status-label">{liveCueLabel}</span>
                    <span class="thinking-status-dots" aria-hidden="true">
                      <span>.</span>
                      <span>.</span>
                      <span>.</span>
                    </span>
                  </div>
                {/if}
              </section>
            {:else}
              <details
                class={getRunClass(item)}
                open={isRunExpanded(item, index)}
                ontoggle={(event) => handleRunToggle(runKey, event)}
              >
                <summary class="run-compact-summary">
                  <span class="run-compact-main">
                    <span class="run-compact-copy">
                      <span class="run-compact-title">{item.summaryTitle ?? 'Run'}</span>
                    </span>
                  </span>

                  <span class="run-compact-metrics">
                    <span class="run-compact-chevron" aria-hidden="true">
                      <ConstellationIcon name="chevron-right" size={14} stroke={1.7} />
                    </span>
                  </span>
                </summary>

                <div class="run-expanded">
                  <header class="run-header">
                    <div class="run-identity">
                      <span class="run-status-mark run-status-mark-header" aria-hidden="true">
                        {getCortexThreadRunStatusGlyph(item.status)}
                      </span>

                      <div class="run-copy">
                        <div class="run-kicker-row">
                          <span class="run-event">{item.event ?? 'Run'}</span>
                          <span class="run-time">{item.timestamp}</span>
                        </div>

                        <div class="run-skill-row">
                          <span class="run-skill">{item.skill}</span>
                          <span class="run-state">
                            {getCortexThreadRunStatusLabel(item.status)}
                          </span>
                        </div>
                      </div>
                    </div>

                    <div class="run-badges">
                      {#if item.model}
                        <ConstellationPill variant="model">{item.model}</ConstellationPill>
                      {/if}

                      {#if item.thinking}
                        <ConstellationPill variant="thinking">thinking: {item.thinking}</ConstellationPill>
                      {/if}

                      {#if item.tokens}
                        <span class="run-meta-badge">{item.tokens}</span>
                      {/if}

                      {#if item.cost}
                        <span class="run-meta-badge">{item.cost}</span>
                      {/if}

                      {#if item.duration}
                        <span class="run-meta-badge">{item.duration}</span>
                      {/if}
                    </div>
                  </header>

                {#if item.telemetry && item.telemetry.length > 0}
                  <div class="run-telemetry-strip" aria-label="Run token telemetry">
                    {#each item.telemetry as metric (metric.label)}
                      <span class="run-telemetry-pill">
                        <span class="run-telemetry-label">{metric.label}</span>
                        <strong class="run-telemetry-value">{metric.value}</strong>
                      </span>
                    {/each}
                  </div>
                {/if}

                {#if item.requiresApproval}
                  <div class="run-approval">
                    <span class="run-approval-label">
                      Approval required before the agent can continue.
                    </span>

                    <div class="run-approval-actions">
                      <ConstellationButton variant="secondary" size="sm" onclick={item.onApprove}>
                        Approve
                      </ConstellationButton>
                      <ConstellationButton variant="quiet" size="sm" onclick={item.onDeny}>
                        Deny
                      </ConstellationButton>
                    </div>
                  </div>
                {/if}

                {#if item.error}
                  <div class="run-error">{item.error}</div>
                {/if}

                <div class="run-sections">
                  {#if item.workItems && item.workItems.length > 0}
                    <div class="run-work-timeline" aria-label="Run work timeline">
                      {#each item.workItems as workItem, workIndex (`${workItem.kind}-${workItem.at ?? workItem.time ?? 'work'}-${workIndex}`)}
                        {#if workItem.kind === 'tool'}
                          <div class={`run-work-item run-work-tool run-work-tool-${workItem.status ?? 'used'}`} title={getTimelineToolTitle(workItem)}>
                            <span class="run-work-tool-icon" aria-hidden="true">
                              {workItem.display?.icon ?? '🔧'}
                            </span>

                            <span class="run-work-tool-copy">
                              <span class="run-work-tool-label">{getTimelineToolLabel(workItem)}</span>
                              {#if getTimelineToolDetail(workItem)}
                                <span class="run-work-tool-detail">{getTimelineToolDetail(workItem)}</span>
                              {/if}
                              {#if shouldShowTimelineToolArgs(workItem)}
                                <code class="run-work-tool-args">{workItem.args}</code>
                              {/if}
                            </span>
                          </div>
                        {:else}
                          <div class={getWorkThoughtClass(workItem.text)}>{@html getWorkThoughtHtml(workItem.text)}</div>
                        {/if}
                      {/each}
                    </div>
                  {/if}

                  {#if orderedSteps.length > 0}
                    <details
                      class={`run-step-strip run-step-strip-${stepTone}`}
                      open={isRunSectionExpanded(runKey, 'graph', item.graphDefaultExpanded)}
                      ontoggle={(event) => handleRunSectionToggle(runKey, 'graph', event)}
                    >
                      <summary class="run-step-summary">
                        <span class="run-step-summary-copy">
                          <span class="run-step-summary-count">
                            {orderedSteps.length} {orderedSteps.length === 1 ? 'step' : 'steps'}
                          </span>
                          <span class="run-step-summary-meta">
                            {summarizeCortexThreadRunSteps(orderedSteps)}
                          </span>
                        </span>

                        <span class="run-step-track" aria-hidden="true">
                          {#each orderedSteps as step}
                            <span
                              class={`run-step-segment run-step-segment-${step.status}`}
                              title={step.task || step.label}
                            ></span>
                          {/each}
                        </span>

                        <span class="run-step-chevron" aria-hidden="true">▾</span>
                      </summary>

                      <div class="run-step-details">
                        {#if item.graphEyebrow}
                          <p class="run-section-eyebrow">{item.graphEyebrow}</p>
                        {/if}

                        {#each orderedSteps as step}
                          <article class={`run-step-row run-step-row-${step.status}`} title={step.task || step.label}>
                            <span class="run-step-mark" aria-hidden="true">
                              {getCortexThreadStepStatusGlyph(step.status)}
                            </span>

                            <span class="run-step-copy">
                              <span class="run-step-heading">
                                <span class="run-step-label">{step.label}</span>

                                {#if typeof step.wave === 'number'}
                                  <span class="run-step-wave">Wave {step.wave + 1}</span>
                                {/if}
                              </span>

                              {#if step.task}
                                <span class="run-step-task">{step.task}</span>
                              {/if}

                              {#if step.skill}
                                <span class="run-step-skill">{step.skill}</span>
                              {/if}
                            </span>

                            {#if step.duration}
                              <span class="run-step-duration">{step.duration}</span>
                            {/if}

                            {#if step.tokens}
                              <span class="run-step-duration">{step.tokens}</span>
                            {/if}
                          </article>
                        {/each}
                      </div>
                    </details>
                  {/if}

                  {#if item.liveLines && item.liveLines.length > 0}
                    <section class="run-live-log">
                      <p class="run-section-eyebrow">{item.liveLinesEyebrow ?? 'Live lines'}</p>

                      <div class="run-live-log-body">
                        {#each item.liveLines as line, lineIndex (`${typeof line === 'string' ? line : line.text}-${lineIndex}`)}
                          {@const entry = normalizeCortexThreadLiveLine(line)}
                          <div class="run-live-line">
                            {#if entry.time}
                              <span class="run-live-line-time">{entry.time}</span>
                            {/if}
                            <span class="run-live-line-text">{entry.text}</span>
                          </div>
                        {/each}
                      </div>
                    </section>
                  {/if}

                  {#if item.toolCalls && item.toolCalls.length > 0}
                    <details
                      class="run-tool-summary"
                      open={isRunSectionExpanded(runKey, 'tools', item.toolCallsDefaultOpen)}
                      ontoggle={(event) => handleRunSectionToggle(runKey, 'tools', event)}
                    >
                      <summary class="run-tool-summary-toggle">
                        <span class="run-tool-summary-label">
                          {item.toolCallsTitle ?? 'Tool calls'}
                          <strong class="run-tool-summary-count">{item.toolCalls.length}</strong>
                        </span>

                        <span class="run-tool-summary-chevron" aria-hidden="true">▾</span>
                      </summary>

                      <div class="run-tool-summary-list">
                        {#each item.toolCalls as call, callIndex (`${call.tool}-${callIndex}`)}
                          <article class="run-tool-summary-item">
                            <div class="run-tool-summary-item-main">
                              <span class="run-tool-summary-tool">
                                <span aria-hidden="true">{call.display?.icon ?? '🔧'}</span>
                                <span>{getToolCallLabel(call)}</span>
                              </span>
                              {#if getToolCallDetail(call)}
                                <span class="run-tool-summary-args">{getToolCallDetail(call)}</span>
                              {/if}
                              {#if call.status}
                                <span class={`run-tool-summary-status run-tool-summary-status-${call.status}`}>
                                  {call.status}
                                </span>
                              {/if}
                            </div>

                            {#if call.at}
                              <span class="run-tool-summary-time">{call.at}</span>
                            {/if}
                          </article>
                        {/each}
                      </div>
                    </details>
                  {/if}

                  {#if item.evidenceDebug}
                    <details
                      class={`run-evidence-debug run-evidence-${item.evidenceDebug.tone}`}
                      open={isRunSectionExpanded(runKey, 'evidence', item.evidenceDebug.defaultOpen)}
                      ontoggle={(event) => handleRunSectionToggle(runKey, 'evidence', event)}
                    >
                      <summary class="run-evidence-summary">
                        <span class="run-evidence-summary-copy">
                          <span class="run-evidence-title">Evidence</span>
                          <span class="run-evidence-subtitle">{item.evidenceDebug.summaryLabel}</span>
                        </span>

                        {#if item.evidenceDebug.verifierLabel}
                          <span class="run-evidence-verifier">{item.evidenceDebug.verifierLabel}</span>
                        {/if}

                        <span class="run-tool-summary-chevron" aria-hidden="true">▾</span>
                      </summary>

                      <div class="run-evidence-body">
                        <div class="run-evidence-meta-grid">
                          {#if item.evidenceDebug.contractType}
                            <div class="run-evidence-meta-item">
                              <span class="run-evidence-meta-label">Contract</span>
                              <strong>{item.evidenceDebug.contractType}</strong>
                            </div>
                          {/if}

                          {#if item.evidenceDebug.contractRequirements.length > 0}
                            <div class="run-evidence-meta-item run-evidence-meta-item-wide">
                              <span class="run-evidence-meta-label">Requirements</span>
                              <div class="run-evidence-chip-row">
                                {#each item.evidenceDebug.contractRequirements as requirement, reqIndex (`${requirement}-${reqIndex}`)}
                                  <span class="run-evidence-chip">{requirement}</span>
                                {/each}
                              </div>
                            </div>
                          {/if}
                        </div>

                        {#if item.evidenceDebug.steps.length > 0}
                          <div class="run-evidence-step-list">
                            {#each item.evidenceDebug.steps as step (step.stepId)}
                              <article class="run-evidence-step">
                                <div class="run-evidence-step-main">
                                  <span class="run-evidence-step-label">{step.label}</span>
                                  <span class="run-evidence-step-count">{step.evidenceLabel}</span>
                                </div>

                                {#if step.tools.length > 0}
                                  <div class="run-evidence-tool-row">
                                    {#each step.tools as tool, toolIndex (`${step.stepId}-${tool}-${toolIndex}`)}
                                      <span class="run-evidence-tool">{tool}</span>
                                    {/each}
                                  </div>
                                {/if}
                              </article>
                            {/each}
                          </div>
                        {/if}

                        {#if item.evidenceDebug.missingEvidence.length > 0 || item.evidenceDebug.unsupportedClaims.length > 0}
                          <div class="run-evidence-findings">
                            {#if item.evidenceDebug.missingEvidence.length > 0}
                              <div class="run-evidence-finding-group">
                                <span class="run-evidence-meta-label">Missing</span>
                                {#each item.evidenceDebug.missingEvidence as finding, findingIndex (`missing-${finding}-${findingIndex}`)}
                                  <span class="run-evidence-finding">{finding}</span>
                                {/each}
                              </div>
                            {/if}

                            {#if item.evidenceDebug.unsupportedClaims.length > 0}
                              <div class="run-evidence-finding-group">
                                <span class="run-evidence-meta-label">Unsupported</span>
                                {#each item.evidenceDebug.unsupportedClaims as claim, claimIndex (`unsupported-${claim}-${claimIndex}`)}
                                  <span class="run-evidence-finding">{claim}</span>
                                {/each}
                              </div>
                            {/if}
                          </div>
                        {/if}
                      </div>
                    </details>
                  {/if}
                </div>
              </div>
            </details>
            {/if}
          {:else if item.kind === 'thinking'}
            {@const thinkingStatusLabel = getThinkingStatusLabel(item)}
            {@const thinkingSteps = getThinkingSteps(item)}
            <section class="thread-thinking-state" aria-live="polite" aria-label={thinkingStatusLabel}>
              <div class="thread-thinking-header">
                <span class="thinking-status-label">{thinkingStatusLabel}</span>
                <span class="thinking-status-dots" aria-hidden="true">
                  <span>.</span>
                  <span>.</span>
                  <span>.</span>
                </span>

                {#if item.toolCount}
                  <span class="thread-thinking-badge">{item.toolCount} tools</span>
                {/if}
              </div>

              {#if thinkingSteps.length > 0}
                <div class="thread-thinking-steps">
                  {#each thinkingSteps as step, stepIndex (`${step.time ?? 'step'}-${stepIndex}`)}
                    <div class="thread-thinking-step">
                      {#if step.time}
                        <span class="thread-thinking-step-time">{step.time}</span>
                      {/if}
                      <span>{step.label}</span>
                    </div>
                  {/each}
                </div>
              {/if}
            </section>
          {/if}
        {/each}
      {:else}
        <div class="thread-empty-state">{emptyLabel}</div>
      {/if}
    </div>

    <ConversationScrollCue visible={showScrollCue} onclick={onScrollToBottom} />
  </div>

  {#if showReplyDock}
    <div class="thread-column thread-composer-dock">
      {#if replyDock}
        {@render replyDock()}
      {:else}
        <div class="thread-reply-dock-placeholder" aria-hidden="true">
          <div class="thread-reply-dock-placeholder-title">{replyPlaceholder}</div>
          <div class="thread-reply-dock-placeholder-hint">{replyHint}</div>
        </div>
      {/if}
    </div>
  {/if}
</section>

{#if previewAttachment && previewAttachmentUrl}
  <AttachmentPreviewDialog
    url={previewAttachmentUrl}
    label={previewAttachmentLabel}
    detail={previewAttachmentDetail}
    kind={previewAttachmentKind}
    openUrl={previewAttachment.downloadUrl || previewAttachmentUrl}
    fallbackIcon={attachmentIconName(previewAttachment)}
    onClose={closeAttachmentPreview}
  />
{/if}

<style>
  .thread-transcript {
    --thread-font-sans: var(--constellation-font-sans, 'Inter', 'Helvetica Neue', Arial, sans-serif);
    --thread-font-mono: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    --thread-radius-panel: var(--constellation-radius-panel, 16px);
    --thread-radius-pill: var(--constellation-radius-pill, 999px);
    --thread-color-text-primary: var(--constellation-color-text-primary, #f0f0fa);
    --thread-color-spectral: var(--constellation-color-spectral, #8db7ff);
    --thread-color-spectral-core: var(--constellation-color-spectral-core, rgba(58, 90, 146, 0.98));
    --thread-color-spectral-owner:
      var(--constellation-color-spectral-owner, rgba(216, 231, 255, 0.96));
    --thread-color-amber: var(--thread-accent, var(--constellation-color-spectral, #57CFA0));
    --thread-color-amber-core: color-mix(in srgb, var(--thread-color-amber) 48%, rgba(5, 9, 16, 0.98));
    --thread-color-amber-owner:
      color-mix(in srgb, var(--thread-color-amber) 34%, rgba(240, 240, 250, 0.96));
    --thread-focus-ring: var(--constellation-control-focus-ring, rgba(240, 240, 250, 0.52));
    --thread-motion-hover-duration: var(--constellation-motion-hover-duration, 180ms);
    --thread-motion-settle-duration: var(--constellation-motion-settle-duration, 240ms);
    --thread-column-max: 860px;
    --thread-message-user-author: rgba(240, 240, 250, 0.68);
    --thread-message-user-meta: rgba(240, 240, 250, 0.58);
    --thread-message-user-body: rgba(240, 240, 250, 0.92);
    --thread-run-border-queued: rgba(255, 255, 255, 0.08);
    --thread-run-border-running: color-mix(in srgb, var(--thread-accent, #57CFA0) 24%, transparent);
    --thread-run-border-completed: rgba(255, 255, 255, 0.05);
    --thread-run-border-failed: rgba(225, 121, 121, 0.24);
    --thread-run-border-attention: rgba(141, 183, 255, 0.24);
    --thread-run-summary-text: rgba(240, 240, 250, 0.82);
    --thread-run-summary-hover-background: rgba(255, 255, 255, 0.025);
    --thread-run-title-text: rgba(255, 255, 255, 0.92);
    --thread-run-state-text: rgba(232, 188, 113, 0.92);
    --thread-run-success-text: rgba(129, 223, 163, 0.96);
    --thread-run-danger-text: rgba(255, 177, 177, 0.94);
    --thread-run-info-text: rgba(171, 203, 255, 0.94);
    --thread-run-muted-text: rgba(240, 240, 250, 0.46);
    --thread-run-primary-text: rgba(240, 240, 250, 0.82);
    --thread-run-chevron-text: rgba(240, 240, 250, 0.44);
    --thread-run-task-text: rgba(240, 240, 250, 0.62);
    --thread-run-pill-border: rgba(255, 255, 255, 0.06);
    --thread-run-pill-background: rgba(255, 255, 255, 0.035);
    --thread-run-pill-text: rgba(240, 240, 250, 0.52);
    --thread-run-strong-pill-border: rgba(141, 183, 255, 0.16);
    --thread-run-strong-pill-background: rgba(141, 183, 255, 0.065);
    --thread-run-strong-pill-text: rgba(171, 203, 255, 0.9);
    --thread-run-telemetry-label: rgba(240, 240, 250, 0.38);
    --thread-run-telemetry-value: rgba(240, 240, 250, 0.68);
    --thread-run-divider-border: rgba(255, 255, 255, 0.05);
    --thread-run-step-summary-border: rgba(255, 255, 255, 0.07);
    --thread-run-step-summary-hover-border: rgba(255, 255, 255, 0.12);
    --thread-run-step-summary-background: rgba(255, 255, 255, 0.03);
    --thread-run-step-segment-background: rgba(255, 255, 255, 0.08);
    --thread-run-step-segment-completed-background: rgba(99, 208, 142, 0.56);
    --thread-run-step-segment-running-background: rgba(141, 183, 255, 0.54);
    --thread-run-step-segment-pending-background: rgba(255, 255, 255, 0.16);
    --thread-run-step-segment-failed-background: rgba(225, 121, 121, 0.6);
    --thread-run-step-segment-skipped-background: rgba(255, 255, 255, 0.04);
    --thread-run-live-log-border: rgba(255, 255, 255, 0.05);
    --thread-run-live-log-background: rgba(8, 11, 18, 0.54);
    --thread-run-live-log-scrollbar: rgba(255, 255, 255, 0.12);
    --thread-work-summary-text: var(--constellation-thread-work-summary-text, rgba(240, 240, 250, 0.52));
    --thread-work-summary-hover-text: var(--constellation-thread-work-summary-hover-text, rgba(240, 240, 250, 0.78));
    --thread-work-divider: var(--constellation-thread-work-divider, rgba(240, 240, 250, 0.08));
    --thread-work-thought-text: rgba(240, 240, 250, 0.74);
    --thread-work-thought-strong-text: rgba(240, 240, 250, 0.88);
    --thread-work-tool-text: var(--constellation-thread-work-tool-text, rgba(240, 240, 250, 0.48));
    --thread-work-tool-icon-text: var(--constellation-thread-work-tool-icon-text, rgba(240, 240, 250, 0.42));
    --thread-work-tool-code-background: var(--constellation-thread-work-tool-code-background, rgba(240, 240, 250, 0.055));
    --thread-work-tool-code-text: var(--constellation-thread-work-tool-code-text, rgba(240, 240, 250, 0.62));
    --thread-work-live-cue-text: var(--constellation-thread-work-live-cue-text, rgba(240, 240, 250, 0.68));
    --thread-run-approval-border: rgba(141, 183, 255, 0.16);
    --thread-run-approval-background: rgba(141, 183, 255, 0.06);
    --thread-run-approval-text: rgba(171, 203, 255, 0.92);
    --thread-run-awaiting-border: rgba(237, 191, 116, 0.18);
    --thread-run-awaiting-background: rgba(237, 191, 116, 0.08);
    --thread-run-awaiting-text: rgba(244, 214, 160, 0.94);
    --thread-run-error-border: rgba(225, 121, 121, 0.18);
    --thread-run-error-background: rgba(225, 121, 121, 0.08);
    --thread-run-error-text: rgba(255, 189, 189, 0.94);
    --thread-run-step-completed-border: rgba(99, 208, 142, 0.2);
    --thread-run-step-running-border: rgba(141, 183, 255, 0.26);
    --thread-run-step-failed-border: rgba(225, 121, 121, 0.24);
    --thread-run-step-completed-text: rgba(129, 223, 163, 0.78);
    --thread-run-step-running-text: rgba(171, 203, 255, 0.82);
    --thread-run-step-failed-text: rgba(255, 177, 177, 0.82);
    --thread-run-evidence-surface-border: rgba(255, 255, 255, 0.055);
    --thread-run-evidence-surface-background: rgba(255, 255, 255, 0.025);
    --thread-run-evidence-chip-border: rgba(255, 255, 255, 0.06);
    --thread-run-evidence-chip-background: rgba(255, 255, 255, 0.035);
    --thread-run-evidence-title-text: rgba(255, 255, 255, 0.9);
    --thread-run-evidence-muted-text: rgba(240, 240, 250, 0.46);
    --thread-run-evidence-subtle-text: rgba(240, 240, 250, 0.38);
    --thread-run-evidence-tool-text: rgba(232, 188, 113, 0.84);
    --thread-run-evidence-finding-text: rgba(255, 190, 190, 0.88);
    --thread-run-evidence-failed-border: rgba(225, 121, 121, 0.22);
    --thread-run-evidence-warning-border: rgba(232, 188, 113, 0.22);
    --thread-run-evidence-passed-border: rgba(99, 208, 142, 0.18);
    --thread-thinking-background: transparent;
    --thread-thinking-dot-background: rgba(240, 240, 250, 0.56);
    --thread-thinking-text: rgba(240, 240, 250, 0.68);
    --thread-thinking-badge-background: rgba(240, 240, 250, 0.06);
    --thread-thinking-step-text: rgba(240, 240, 250, 0.54);
    --thread-thinking-step-time-text: rgba(240, 240, 250, 0.32);
    --thread-reply-placeholder-border: rgba(255, 255, 255, 0.06);
    --thread-reply-placeholder-background: linear-gradient(180deg, rgba(13, 17, 26, 0.72), rgba(8, 11, 18, 0.82));
    --thread-reply-placeholder-shadow:
      0 18px 40px rgba(0, 0, 0, 0.18),
      0 0 0 1px rgba(255, 255, 255, 0.02) inset;
    --thread-reply-placeholder-title: rgba(240, 240, 250, 0.88);
    --thread-reply-placeholder-hint: rgba(240, 240, 250, 0.46);
    --thread-composer-clearance: clamp(22px, 2.4vh, 34px);
    --thread-composer-inline-padding: 18px;
    --thread-transient-rail-offset: 0px;

    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto;
    width: 100%;
    flex: 1 1 auto;
    height: 100%;
    min-height: 0;
    min-width: 0;
    position: relative;
    isolation: isolate;
  }

  :global(:root[data-color-scheme='light']) .thread-transcript {
    --thread-message-user-author: rgba(49, 63, 76, 0.72);
    --thread-message-user-meta: rgba(82, 98, 111, 0.68);
    --thread-message-user-body: rgba(28, 40, 53, 0.94);
    --thread-run-border-queued: rgba(26, 39, 49, 0.14);
    --thread-run-border-running: color-mix(in srgb, var(--thread-accent, #57CFA0) 28%, transparent);
    --thread-run-border-completed: rgba(20, 120, 93, 0.26);
    --thread-run-border-failed: rgba(178, 74, 97, 0.28);
    --thread-run-border-attention: rgba(49, 95, 214, 0.28);
    --thread-run-summary-text: rgba(32, 45, 59, 0.86);
    --thread-run-summary-hover-background: rgba(255, 253, 247, 0.62);
    --thread-run-title-text: rgba(18, 27, 36, 0.94);
    --thread-run-state-text: #7c4617;
    --thread-run-success-text: #0f5f4a;
    --thread-run-danger-text: #8d3148;
    --thread-run-info-text: #244fae;
    --thread-run-muted-text: rgba(82, 98, 111, 0.66);
    --thread-run-primary-text: rgba(32, 45, 59, 0.84);
    --thread-run-chevron-text: rgba(82, 98, 111, 0.62);
    --thread-run-task-text: rgba(49, 63, 76, 0.76);
    --thread-run-pill-border: rgba(26, 39, 49, 0.13);
    --thread-run-pill-background: rgba(248, 250, 248, 0.78);
    --thread-run-pill-text: rgba(49, 63, 76, 0.78);
    --thread-run-strong-pill-border: rgba(49, 95, 214, 0.2);
    --thread-run-strong-pill-background: rgba(49, 95, 214, 0.09);
    --thread-run-strong-pill-text: #244fae;
    --thread-run-telemetry-label: rgba(82, 98, 111, 0.7);
    --thread-run-telemetry-value: rgba(18, 27, 36, 0.86);
    --thread-run-divider-border: rgba(26, 39, 49, 0.1);
    --thread-run-step-summary-border: rgba(26, 39, 49, 0.13);
    --thread-run-step-summary-hover-border: rgba(26, 39, 49, 0.2);
    --thread-run-step-summary-background: rgba(248, 250, 248, 0.78);
    --thread-run-step-segment-background: rgba(26, 39, 49, 0.14);
    --thread-run-step-segment-completed-background: rgba(20, 120, 93, 0.58);
    --thread-run-step-segment-running-background: rgba(49, 95, 214, 0.58);
    --thread-run-step-segment-pending-background: rgba(26, 39, 49, 0.22);
    --thread-run-step-segment-failed-background: rgba(178, 74, 97, 0.58);
    --thread-run-step-segment-skipped-background: rgba(26, 39, 49, 0.08);
    --thread-run-live-log-border: rgba(26, 39, 49, 0.11);
    --thread-run-live-log-background: rgba(231, 240, 244, 0.86);
    --thread-run-live-log-scrollbar: rgba(26, 39, 49, 0.18);
    --thread-run-approval-border: rgba(49, 95, 214, 0.22);
    --thread-run-approval-background: rgba(49, 95, 214, 0.09);
    --thread-run-approval-text: #244fae;
    --thread-run-awaiting-border: color-mix(in srgb, var(--thread-accent, #57CFA0) 22%, transparent);
    --thread-run-awaiting-background: color-mix(in srgb, var(--thread-accent, #57CFA0) 9%, transparent);
    --thread-run-awaiting-text: #7c4617;
    --thread-run-error-border: rgba(178, 74, 97, 0.22);
    --thread-run-error-background: rgba(178, 74, 97, 0.09);
    --thread-run-error-text: #8d3148;
    --thread-run-step-completed-border: rgba(20, 120, 93, 0.26);
    --thread-run-step-running-border: rgba(49, 95, 214, 0.28);
    --thread-run-step-failed-border: rgba(178, 74, 97, 0.26);
    --thread-run-step-completed-text: #0f5f4a;
    --thread-run-step-running-text: #244fae;
    --thread-run-step-failed-text: #8d3148;
    --thread-run-evidence-surface-border: rgba(24, 35, 49, 0.08);
    --thread-run-evidence-surface-background: rgba(255, 255, 255, 0.46);
    --thread-run-evidence-chip-border: rgba(24, 35, 49, 0.08);
    --thread-run-evidence-chip-background: rgba(255, 255, 255, 0.46);
    --thread-run-evidence-title-text: rgba(17, 24, 35, 0.9);
    --thread-run-evidence-muted-text: rgba(78, 91, 108, 0.56);
    --thread-run-evidence-subtle-text: rgba(78, 91, 108, 0.56);
    --thread-run-evidence-tool-text: rgba(184, 135, 69, 0.9);
    --thread-run-evidence-finding-text: #a84a5d;
    --thread-run-evidence-failed-border: rgba(195, 95, 113, 0.2);
    --thread-run-evidence-warning-border: rgba(184, 135, 69, 0.2);
    --thread-run-evidence-passed-border: rgba(63, 139, 120, 0.18);
    --thread-work-live-cue-text: rgba(32, 45, 59, 0.66);
    --thread-work-thought-text: rgba(32, 45, 59, 0.76);
    --thread-work-thought-strong-text: rgba(18, 27, 36, 0.9);
    --thread-thinking-background: transparent;
    --thread-thinking-dot-background: rgba(32, 45, 59, 0.52);
    --thread-thinking-text: rgba(32, 45, 59, 0.66);
    --thread-thinking-badge-background: rgba(26, 39, 49, 0.06);
    --thread-thinking-step-text: rgba(82, 98, 111, 0.66);
    --thread-thinking-step-time-text: rgba(82, 98, 111, 0.52);
    --thread-reply-placeholder-border: rgba(126, 92, 52, 0.1);
    --thread-reply-placeholder-background: rgba(255, 253, 247, 0.9);
    --thread-reply-placeholder-shadow: 0 18px 40px rgba(54, 70, 82, 0.08);
    --thread-reply-placeholder-title: rgba(32, 45, 59, 0.84);
    --thread-reply-placeholder-hint: rgba(82, 98, 111, 0.66);
  }

  .thread-column {
    width: min(100%, var(--thread-column-max));
    margin-inline: auto;
    box-sizing: border-box;
  }

  .thread-panel-header {
    --thread-header-status-halo-clearance: 8px;
    display: grid;
    overflow: visible;
    padding:
      var(--thread-header-status-halo-clearance)
      2px
      14px
      var(--thread-header-status-halo-clearance);
  }

  .thread-header-title-row {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 32px;
    min-width: 0;
  }

  .thread-header-title-row :global(.thread-header-status-indicator) {
    --constellation-signal-status-color: var(--thread-accent, var(--thread-color-spectral));
    --constellation-signal-status-idle-color: var(--constellation-thread-header-status-idle-dot);
    --constellation-signal-status-idle-ring: var(--constellation-thread-header-status-idle-ring);
  }

  .thread-header-title {
    flex: 0 1 auto;
    min-width: 0;
    margin: 0;
    color: var(--constellation-thread-header-title);
    font-family: var(--thread-font-sans);
    font-size: clamp(15px, 1.35vw, 18px);
    font-weight: 560;
    line-height: 1.28;
    letter-spacing: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .thread-header-title-row :global(.thread-header-action-button) {
    flex: 0 0 auto;
  }

  .thread-header-title-row :global(.thread-header-action-button:hover:not(:disabled)) {
    transform: none;
  }

  .thread-header-title-row :global(.thread-archive-button) {
    color: var(--constellation-button-destructive-text);
  }

  .thread-header-title-row :global(.thread-archive-button:hover:not(:disabled)),
  .thread-header-title-row :global(.thread-archive-button:focus-visible) {
    color: var(--constellation-button-destructive-text);
  }

  .thread-header-title-row :global(.thread-archive-button:active:not(:disabled)),
  .thread-header-title-row :global(.thread-archive-button.is-loading) {
    color: var(--constellation-button-destructive-pressed-text);
  }

  .thread-header-title-row :global(.thread-title-action-button.is-loading svg) {
    animation: thread-title-action-spin 720ms linear infinite;
  }

  .thread-header-panel-toggle-group {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;
    margin-right: -2px;
    padding: 3px 2px 2px;
  }

  @keyframes thread-title-action-spin {
    to {
      transform: rotate(360deg);
    }
  }

  .thread-content {
    position: relative;
    min-height: 0;
    overflow: auto;
    padding: 12px 0 0;
    scroll-padding-bottom: var(--thread-composer-clearance);
    scrollbar-width: none;
  }

  .thread-content::-webkit-scrollbar {
    display: none;
  }

  .message-stack {
    display: grid;
    gap: 18px;
    padding-right: 0;
    padding-bottom: var(--thread-composer-clearance);
  }

  .thread-empty-state {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 160px;
    padding: 18px;
    border-radius: 20px;
    border: 1px solid var(--constellation-thread-empty-border);
    background: var(--constellation-thread-empty-background);
    color: var(--constellation-thread-empty-text);
    font-family: var(--thread-font-sans);
    font-size: 14px;
    line-height: 1.5;
    text-align: center;
  }

  .thread-message,
  .run-insert,
  .run-live-work-stream,
  .thread-thinking-state {
    width: min(100%, 760px);
  }

  .thread-message {
    position: relative;
    display: grid;
    gap: 10px;
    /* Own these box metrics so legacy global .thread-message styles cannot indent Illo replies. */
    margin-bottom: 0;
    padding: 0;
    border-radius: 0;
    color: var(--thread-color-text-primary);
  }

  .thread-message > * {
    position: relative;
    z-index: 1;
  }

  .thread-message-illo {
    --thread-message-accent: var(--constellation-thread-message-illo-accent);
    --thread-message-core: var(--constellation-thread-message-illo-core);
    --thread-message-owner: var(--constellation-thread-message-illo-owner);
    --thread-message-body: var(--constellation-thread-message-illo-body);
    --thread-message-meta: var(--constellation-thread-message-illo-meta);
    justify-self: start;
    margin-right: auto;
  }

  .thread-message-inline-work.thread-message-illo {
    gap: 8px;
  }

  .thread-message-user {
    --thread-message-accent: var(--thread-color-spectral);
    --thread-message-core: var(--thread-color-spectral-core);
    --thread-message-owner: var(--thread-color-spectral-owner);
    --thread-message-shell: var(--constellation-thread-message-user-shell-base);
    --thread-message-border: var(--constellation-thread-message-user-border-base);
    --thread-message-meta: var(--thread-message-user-meta);
    --thread-message-body: var(--thread-message-user-body);
    width: fit-content;
    max-width: min(100%, 620px);
    justify-self: end;
    margin-left: auto;
    padding: 14px 16px 16px;
    border-radius: var(--thread-radius-panel);
    border: 1px solid var(--thread-message-border);
    background: var(--thread-message-shell);
    box-shadow: var(--constellation-thread-message-user-shadow);
    isolation: isolate;
    box-sizing: border-box;
  }

  .thread-message-spectral.thread-message-user {
    --thread-message-accent: var(--thread-color-spectral);
    --thread-message-core: var(--thread-color-spectral-core);
    --thread-message-owner: var(--thread-color-spectral-owner);
  }

  .thread-message-amber.thread-message-user {
    --thread-message-accent: var(--thread-color-amber);
    --thread-message-core: var(--thread-color-amber-core);
    --thread-message-owner: var(--thread-color-amber-owner);
  }

  .thread-message-header {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  .thread-message-meta {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    min-width: 0;
  }

  .thread-message-author,
  .thread-message-meta-supplemental {
    font-family: var(--thread-font-mono);
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .thread-message-author {
    color: var(--constellation-thread-message-author);
  }

  .thread-message-user .thread-message-author {
    color: var(--thread-message-user-author);
  }

  .thread-message-meta-supplemental {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 7px;
    color: var(--thread-message-meta);
  }

  .thread-message-meta-divider {
    width: 3px;
    height: 3px;
    border-radius: 50%;
    background: currentColor;
  }

  .thread-message-content {
    display: grid;
    gap: 12px;
    color: var(--thread-message-body);
    font-family: var(--thread-font-sans);
    font-size: 14px;
    line-height: 1.58;
  }

  .thread-message-illo .thread-message-content {
    color: var(--thread-message-body);
  }

  .thread-message-content > * {
    margin: 0;
  }

  .thread-message-html {
    --constellation-prose-text: var(--thread-message-body);
    --constellation-prose-heading: var(--constellation-thread-message-heading);
    --constellation-prose-muted: var(--thread-message-meta);
    --constellation-prose-font-size: 14px;
    --constellation-prose-line-height: 1.62;
  }

  .thread-message-content ul,
  .thread-message-content ol {
    margin: 0;
    padding-left: 18px;
  }

  .thread-message-content li + li {
    margin-top: 4px;
  }

  .thread-message-content h2 {
    color: var(--constellation-thread-message-heading);
    font-family: var(--thread-font-sans);
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0;
    line-height: 1.35;
    text-transform: none;
  }

  .thread-message-attachments {
    display: grid;
    gap: 10px;
  }

  .thread-message-thread-previews {
    display: grid;
    gap: 8px;
  }

  .thread-message-image-button {
    appearance: none;
    display: block;
    width: 100%;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: zoom-in;
    text-align: left;
  }

  .thread-message-image {
    width: 100%;
    max-width: 100%;
    display: block;
    border-radius: 16px;
    border: 1px solid var(--constellation-thread-message-image-border);
    background: var(--constellation-thread-message-image-background);
    transition: transform 180ms ease;
  }

  .thread-message-image-button:hover .thread-message-image,
  .thread-message-image-button:focus-visible .thread-message-image {
    transform: scale(1.006);
  }

  .thread-message-file {
    appearance: none;
    display: inline-flex;
    align-items: center;
    justify-content: flex-start;
    gap: 10px;
    width: min(100%, 420px);
    max-width: 100%;
    padding: 10px 12px;
    border-radius: 12px;
    border: 1px solid var(--constellation-thread-message-file-border);
    background: var(--constellation-thread-message-file-background);
    color: var(--constellation-thread-message-file-text);
    cursor: zoom-in;
    font-family: var(--thread-font-mono);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-align: left;
    text-decoration: none;
    text-transform: uppercase;
    transition:
      background-color 160ms ease,
      border-color 160ms ease,
      transform 160ms ease;
  }

  .thread-message-file:hover,
  .thread-message-file:focus-visible {
    transform: translateY(-1px);
  }

  .thread-message-file:focus-visible,
  .thread-message-image-button:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .thread-message-file-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    width: 32px;
    height: 32px;
    border-radius: 10px;
    border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
    background: color-mix(in srgb, currentColor 8%, transparent);
  }

  .thread-message-file-copy {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .thread-message-file-copy span,
  .thread-message-file-copy small {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .thread-message-file-copy small {
    color: color-mix(in srgb, currentColor 64%, transparent);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0;
    text-transform: none;
  }

  .thread-visual-surface {
    width: min(100%, 760px);
  }

  .run-insert {
    display: grid;
    gap: 0;
    overflow: visible;
    border-radius: 0;
    border: 0;
    background: transparent;
    box-shadow: none;
  }

  .run-live-work-stream {
    display: grid;
    gap: 12px;
    justify-self: start;
    margin-right: auto;
    box-sizing: border-box;
    padding-inline-start: var(--thread-transient-rail-offset);
  }

  .run-queued {
    border-color: var(--thread-run-border-queued);
  }

  .run-running {
    border-color: var(--thread-run-border-running);
  }

  .run-completed {
    border-color: var(--thread-run-border-completed);
  }

  .run-failed {
    border-color: var(--thread-run-border-failed);
  }

  .run-canceled {
    border-color: var(--thread-run-border-failed);
  }

  .run-pending_approval {
    border-color: var(--thread-run-border-attention);
  }

  .run-compact-summary {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 0 0 8px;
    border-bottom: 1px solid var(--thread-work-divider);
    list-style: none;
    cursor: pointer;
    color: var(--thread-work-summary-text);
    text-align: left;
    transition:
      border-color var(--thread-motion-hover-duration) ease,
      color var(--thread-motion-hover-duration) ease;
  }

  .run-compact-summary::-webkit-details-marker {
    display: none;
  }

  .run-compact-summary:hover {
    color: var(--thread-work-summary-hover-text);
  }

  .run-compact-summary:focus-visible {
    outline: 2px solid var(--thread-focus-ring);
    outline-offset: -2px;
  }

  .run-compact-main,
  .run-compact-copy,
  .run-compact-metrics {
    display: flex;
    align-items: center;
  }

  .run-compact-main {
    min-width: 0;
    gap: 0;
    flex: 1 1 auto;
  }

  .run-compact-copy {
    min-width: 0;
    flex-wrap: wrap;
    gap: 6px 8px;
  }

  .run-compact-title {
    color: currentColor;
    font-family: var(--thread-font-sans);
    font-size: 13px;
    font-weight: 400;
    line-height: 1.35;
  }

  .run-compact-state,
  .run-compact-time,
  .run-compact-metric {
    font-family: var(--thread-font-mono);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .run-compact-state {
    color: var(--thread-run-state-text);
  }

  .run-completed .run-compact-state {
    color: var(--thread-run-success-text);
  }

  .run-failed .run-compact-state {
    color: var(--thread-run-danger-text);
  }

  .run-canceled .run-compact-state {
    color: var(--thread-run-danger-text);
  }

  .run-pending_approval .run-compact-state {
    color: var(--thread-run-info-text);
  }

  .run-compact-time {
    color: var(--thread-run-muted-text);
  }

  .run-compact-metrics {
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0;
  }

  .run-compact-metric {
    display: inline-flex;
    align-items: center;
    padding: 4px 8px;
    border-radius: 999px;
    border: 1px solid var(--thread-run-pill-border);
    background: var(--thread-run-pill-background);
    color: var(--thread-run-pill-text);
  }

  .run-compact-metric-strong {
    border-color: var(--thread-run-strong-pill-border);
    background: var(--thread-run-strong-pill-background);
    color: var(--thread-run-strong-pill-text);
  }

  .run-compact-chevron {
    flex-shrink: 0;
    display: inline-flex;
    color: currentColor;
    transition: transform var(--thread-motion-hover-duration) ease;
  }

  details[open] > .run-compact-summary .run-compact-chevron {
    transform: rotate(90deg);
  }

  .run-expanded {
    display: grid;
    gap: 14px;
    padding: 14px 0 0;
    border-top: 0;
  }

  .run-header {
    display: flex;
    flex-wrap: wrap;
    align-items: start;
    justify-content: space-between;
    gap: 10px 14px;
  }

  .run-identity {
    display: flex;
    align-items: start;
    gap: 10px;
    min-width: 0;
  }

  .run-status-mark {
    width: 18px;
    height: 18px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--thread-run-state-text);
    font-family: var(--thread-font-mono);
    font-size: 11px;
    line-height: 1;
  }

  .run-status-mark-header {
    margin-top: 1px;
  }

  .run-completed .run-status-mark {
    color: var(--thread-run-success-text);
  }

  .run-failed .run-status-mark {
    color: var(--thread-run-danger-text);
  }

  .run-pending_approval .run-status-mark {
    color: var(--thread-run-info-text);
  }

  .run-copy {
    min-width: 0;
    display: grid;
    gap: 4px;
  }

  .run-kicker-row,
  .run-skill-row,
  .run-approval {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .run-event,
  .run-time,
  .run-state,
  .run-meta-badge,
  .run-approval-label {
    font-family: var(--thread-font-mono);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .run-event {
    color: var(--thread-run-muted-text);
  }

  .run-time {
    color: var(--thread-run-muted-text);
  }

  .run-skill {
    color: var(--thread-run-title-text);
    font-family: var(--thread-font-sans);
    font-size: 14px;
    font-weight: 580;
    line-height: 1.25;
  }

  .run-state {
    color: var(--thread-run-state-text);
  }

  .run-completed .run-state {
    color: var(--thread-run-success-text);
  }

  .run-failed .run-state {
    color: var(--thread-run-danger-text);
  }

  .run-pending_approval .run-state {
    color: var(--thread-run-info-text);
  }

  .run-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }

  .run-badges :global(.constellation-pill) {
    gap: 6px;
    padding: 4px 8px;
    font-size: 10px;
    letter-spacing: 0.1em;
  }

  .run-meta-badge {
    display: inline-flex;
    align-items: center;
    padding: 4px 8px;
    border-radius: 999px;
    border: 1px solid var(--thread-run-pill-border);
    background: var(--thread-run-pill-background);
    color: var(--thread-run-pill-text);
    font-size: 9px;
    letter-spacing: 0.08em;
  }

  .run-telemetry-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .run-telemetry-pill {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    min-width: 0;
    padding: 6px 8px;
    border-radius: 999px;
    border: 1px solid var(--thread-run-strong-pill-border);
    background: var(--thread-run-strong-pill-background);
  }

  .run-telemetry-label,
  .run-telemetry-value {
    font-family: var(--thread-font-mono);
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .run-telemetry-label {
    color: var(--thread-run-telemetry-label);
  }

  .run-telemetry-value {
    color: var(--thread-run-telemetry-value);
    font-weight: 520;
  }

  .run-approval {
    justify-content: space-between;
    gap: 10px 16px;
    padding: 10px 12px;
    border-radius: 14px;
    border: 1px solid var(--thread-run-approval-border);
    background: var(--thread-run-approval-background);
  }

  .run-approval-label {
    color: var(--thread-run-approval-text);
  }

  .run-approval-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .run-awaiting-user {
    justify-content: space-between;
    gap: 10px 16px;
    padding: 10px 12px;
    border-radius: 14px;
    border: 1px solid var(--thread-run-awaiting-border);
    background: var(--thread-run-awaiting-background);
  }

  .run-awaiting-user-label {
    color: var(--thread-run-awaiting-text);
  }

  .run-error {
    padding: 10px 12px;
    border-radius: 14px;
    border: 1px solid var(--thread-run-error-border);
    background: var(--thread-run-error-background);
    color: var(--thread-run-error-text);
    font-family: var(--thread-font-sans);
    font-size: 12px;
    line-height: 1.5;
  }

  .run-sections {
    display: grid;
    gap: 14px;
  }

  .run-with-work-timeline .run-expanded .run-header,
  .run-with-work-timeline .run-expanded .run-telemetry-strip,
  .run-with-work-timeline .run-expanded .run-step-strip,
  .run-with-work-timeline .run-expanded .run-live-log,
  .run-with-work-timeline .run-expanded .run-tool-summary,
  .run-with-work-timeline .run-expanded .run-evidence-debug {
    display: none;
  }

  .run-work-timeline {
    display: grid;
    gap: 14px;
  }

  .run-live-work-cue {
    width: fit-content;
    display: inline-flex;
    align-items: center;
    gap: 0;
    min-height: 22px;
    color: var(--thread-work-live-cue-text);
    font-family: var(--thread-font-sans);
    font-size: 13px;
    font-weight: 440;
    letter-spacing: 0;
    line-height: 1.4;
    text-transform: none;
  }

  .thinking-status-label {
    color: currentColor;
  }

  .thinking-status-dots {
    display: inline-flex;
    align-items: center;
    flex: 0 0 auto;
    width: 1.08em;
    margin-left: 1px;
    color: var(--thread-thinking-dot-background);
  }

  .thinking-status-dots span {
    display: inline-block;
    animation: thinking-dot-wave 1.35s ease-in-out infinite;
  }

  .thinking-status-dots span:nth-child(2) {
    animation-delay: 0.18s;
  }

  .thinking-status-dots span:nth-child(3) {
    animation-delay: 0.36s;
  }

  .run-work-item {
    margin: 0;
    min-width: 0;
  }

  .run-work-thought {
    color: var(--thread-work-thought-text);
    font-family: var(--thread-font-sans);
    font-size: 13.5px;
    font-weight: 400;
    line-height: 1.55;
  }

  .run-work-thought :global(*) {
    margin: 0;
  }

  .run-work-thought :global(p + p) {
    margin-top: 6px;
  }

  .run-work-thought :global(strong) {
    color: var(--thread-work-thought-strong-text);
    font-weight: 560;
  }

  .run-work-thought :global(em) {
    font-style: normal;
    color: var(--thread-work-thought-strong-text);
  }

  .run-work-reflection {
    color: color-mix(in srgb, var(--thread-work-thought-text) 88%, transparent);
  }

  .run-work-tool {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    color: var(--thread-work-tool-text);
    font-family: var(--thread-font-sans);
    font-size: 12.5px;
    line-height: 1.45;
  }

  .run-work-tool-icon {
    width: 15px;
    height: 15px;
    flex: 0 0 auto;
    margin-top: 2px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--thread-work-tool-icon-text);
  }

  .run-work-tool-copy {
    min-width: 0;
    display: inline-flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px;
  }

  .run-work-tool-label {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .run-work-tool-detail {
    min-width: 0;
    color: var(--thread-run-muted-text);
    font-size: 11.5px;
    overflow-wrap: anywhere;
  }

  .run-work-tool-args {
    max-width: 100%;
    overflow: hidden;
    padding: 1px 5px;
    border-radius: 5px;
    background: var(--thread-work-tool-code-background);
    color: var(--thread-work-tool-code-text);
    font-family: var(--thread-font-mono);
    font-size: 11px;
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-section-eyebrow {
    margin: 0;
    color: var(--thread-run-muted-text);
    font-family: var(--thread-font-mono);
    font-size: 10px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .run-step-strip,
  .run-tool-summary,
  .run-evidence-debug {
    display: grid;
    gap: 8px;
  }

  .run-step-summary,
  .run-tool-summary-toggle,
  .run-evidence-summary {
    list-style: none;
    cursor: pointer;
  }

  .run-step-summary::-webkit-details-marker,
  .run-tool-summary-toggle::-webkit-details-marker,
  .run-evidence-summary::-webkit-details-marker {
    display: none;
  }

  .run-step-summary {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border: 1px solid var(--thread-run-step-summary-border);
    border-radius: 8px;
    background: var(--thread-run-step-summary-background);
    color: var(--thread-run-primary-text);
    text-align: left;
    transition:
      border-color 180ms ease,
      background-color 180ms ease,
      opacity 180ms ease;
  }

  .run-step-summary:hover {
    border-color: var(--thread-run-step-summary-hover-border);
  }

  .run-step-summary-copy {
    min-width: 0;
    flex: 0 1 172px;
    display: grid;
    gap: 2px;
  }

  .run-step-summary-count {
    overflow: hidden;
    color: var(--thread-run-title-text);
    font-family: var(--thread-font-sans);
    font-size: 12px;
    font-weight: 550;
    line-height: 1.2;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-step-summary-meta {
    overflow: hidden;
    color: var(--thread-run-muted-text);
    font-family: var(--thread-font-mono);
    font-size: 9px;
    letter-spacing: 0.08em;
    text-overflow: ellipsis;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .run-step-track {
    min-width: 72px;
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    gap: 3px;
  }

  .run-step-segment {
    flex: 1 1 0;
    min-width: 10px;
    height: 4px;
    border-radius: 999px;
    background: var(--thread-run-step-segment-background);
  }

  .run-step-segment-completed {
    background: var(--thread-run-step-segment-completed-background);
  }

  .run-step-segment-running {
    background: var(--thread-run-step-segment-running-background);
    animation: step-pulse 2.4s ease-in-out infinite;
  }

  .run-step-segment-pending {
    background: var(--thread-run-step-segment-pending-background);
  }

  .run-step-segment-failed {
    background: var(--thread-run-step-segment-failed-background);
  }

  .run-step-segment-skipped {
    background: var(--thread-run-step-segment-skipped-background);
  }

  .run-step-chevron,
  .run-tool-summary-chevron {
    flex-shrink: 0;
    color: rgba(240, 240, 250, 0.44);
    font-size: 12px;
    transition: transform 180ms ease;
  }

  details[open] > .run-step-summary .run-step-chevron,
  details[open] > .run-tool-summary-toggle .run-tool-summary-chevron,
  details[open] > .run-evidence-summary .run-tool-summary-chevron {
    transform: rotate(180deg);
  }

  .run-step-strip-completed .run-step-summary {
    border-color: var(--thread-run-step-completed-border);
  }

  .run-step-strip-running .run-step-summary {
    border-color: var(--thread-run-step-running-border);
  }

  .run-step-strip-failed .run-step-summary {
    border-color: var(--thread-run-step-failed-border);
  }

  .run-step-strip-completed .run-step-summary-meta {
    color: var(--thread-run-step-completed-text);
  }

  .run-step-strip-running .run-step-summary-meta,
  .run-step-strip-running .run-step-chevron {
    color: var(--thread-run-step-running-text);
  }

  .run-step-strip-failed .run-step-summary-meta,
  .run-step-strip-failed .run-step-chevron {
    color: var(--thread-run-step-failed-text);
  }

  .run-step-details {
    display: grid;
    gap: 0;
  }

  .run-step-details .run-section-eyebrow {
    margin-bottom: 6px;
  }

  .run-step-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: start;
    gap: 8px;
    padding: 8px 0;
    color: var(--thread-run-primary-text);
  }

  .run-step-row + .run-step-row {
    border-top: 1px solid var(--thread-run-divider-border);
  }

  .run-step-copy {
    min-width: 0;
    display: grid;
    gap: 3px;
  }

  .run-step-mark {
    width: 16px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: currentColor;
    font-family: var(--thread-font-mono);
    font-size: 10px;
    line-height: 1;
  }

  .run-step-heading {
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }

  .run-step-label {
    overflow: hidden;
    color: var(--thread-run-title-text);
    font-family: var(--thread-font-sans);
    font-size: 12px;
    font-weight: 550;
    line-height: 1.2;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-step-wave,
  .run-step-skill,
  .run-step-duration {
    color: var(--thread-run-muted-text);
    font-family: var(--thread-font-mono);
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .run-step-wave,
  .run-step-duration {
    white-space: nowrap;
  }

  .run-step-task {
    color: var(--thread-run-task-text);
    font-family: var(--thread-font-sans);
    font-size: 11px;
    line-height: 1.35;
  }

  .run-step-row-completed .run-step-mark,
  .run-step-row-completed .run-step-label {
    color: var(--thread-run-success-text);
  }

  .run-step-row-running .run-step-mark,
  .run-step-row-running .run-step-label {
    color: var(--thread-run-info-text);
  }

  .run-step-row-pending {
    opacity: 0.78;
  }

  .run-step-row-failed .run-step-mark,
  .run-step-row-failed .run-step-label,
  .run-step-row-failed .run-step-duration {
    color: var(--thread-run-danger-text);
  }

  .run-step-row-skipped {
    opacity: 0.48;
  }

  .run-live-log {
    display: grid;
    gap: 8px;
  }

  .run-live-log-body {
    display: grid;
    gap: 4px;
    max-height: 128px;
    overflow-y: auto;
    padding: 10px 12px;
    border-radius: 14px;
    border: 1px solid var(--thread-run-live-log-border);
    background: var(--thread-run-live-log-background);
  }

  .run-live-log-body::-webkit-scrollbar {
    width: 4px;
  }

  .run-live-log-body::-webkit-scrollbar-thumb {
    border-radius: 999px;
    background: var(--thread-run-live-log-scrollbar);
  }

  .run-live-line {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 8px;
    align-items: start;
    color: var(--thread-run-muted-text);
    font-family: var(--thread-font-mono);
    font-size: 10px;
    line-height: 1.55;
  }

  .run-live-line-time {
    color: var(--thread-run-muted-text);
    white-space: nowrap;
  }

  .run-live-line-text {
    min-width: 0;
  }

  .run-tool-summary-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--thread-run-primary-text);
  }

  .run-tool-summary-toggle:hover {
    color: var(--thread-run-title-text);
  }

  .run-tool-summary-toggle:focus-visible {
    outline: 2px solid var(--thread-focus-ring);
    outline-offset: 4px;
  }

  .run-tool-summary-label {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--thread-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .run-tool-summary-count {
    color: var(--thread-run-state-text);
    font-weight: 600;
  }

  .run-tool-summary-list {
    display: grid;
    gap: 6px;
  }

  .run-tool-summary-item {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px;
    align-items: start;
    padding: 8px 10px;
    border-radius: 12px;
    border: 1px solid var(--thread-run-pill-border);
    background: var(--thread-run-pill-background);
  }

  .run-tool-summary-item-main {
    min-width: 0;
    display: grid;
    gap: 4px;
  }

  .run-tool-summary-time {
    font-family: var(--thread-font-mono);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .run-tool-summary-tool {
    display: inline-flex;
    min-width: 0;
    align-items: baseline;
    gap: 6px;
    color: var(--thread-run-state-text);
    font-family: var(--thread-font-sans);
    font-size: 12px;
    font-weight: 550;
    letter-spacing: 0;
    text-transform: none;
  }

  .run-tool-summary-time {
    color: var(--thread-run-muted-text);
    white-space: nowrap;
  }

  .run-tool-summary-args {
    overflow: hidden;
    color: var(--thread-run-primary-text);
    font-family: var(--thread-font-mono);
    font-size: 10px;
    line-height: 1.5;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-tool-summary-status {
    width: fit-content;
    padding: 2px 6px;
    border-radius: 999px;
    border: 1px solid var(--thread-run-pill-border);
    color: var(--thread-run-muted-text);
    font-family: var(--thread-font-mono);
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .run-tool-summary-status-running {
    color: var(--thread-run-info-text);
  }

  .run-tool-summary-status-completed {
    color: var(--thread-run-success-text);
  }

  .run-tool-summary-status-failed {
    color: var(--thread-run-danger-text);
  }

  .run-evidence-summary {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--thread-run-evidence-surface-border);
    background: var(--thread-run-evidence-surface-background);
    color: var(--thread-run-primary-text);
    transition:
      border-color 180ms ease,
      background-color 180ms ease;
  }

  .run-evidence-summary:hover {
    border-color: var(--thread-run-step-summary-hover-border);
    background: var(--thread-run-evidence-chip-background);
  }

  .run-evidence-summary:focus-visible {
    outline: 2px solid var(--thread-focus-ring);
    outline-offset: 4px;
  }

  .run-evidence-summary-copy {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .run-evidence-title,
  .run-evidence-meta-item strong,
  .run-evidence-step-label {
    overflow: hidden;
    color: var(--thread-run-evidence-title-text);
    font-family: var(--thread-font-sans);
    font-size: 12px;
    font-weight: 560;
    line-height: 1.2;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-evidence-subtitle,
  .run-evidence-verifier,
  .run-evidence-meta-label,
  .run-evidence-step-count,
  .run-evidence-tool,
  .run-evidence-chip {
    font-family: var(--thread-font-mono);
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .run-evidence-subtitle {
    overflow: hidden;
    color: var(--thread-run-evidence-muted-text);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-evidence-verifier {
    max-width: 220px;
    overflow: hidden;
    color: var(--thread-run-evidence-muted-text);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-evidence-failed .run-evidence-summary {
    border-color: var(--thread-run-evidence-failed-border);
  }

  .run-evidence-failed .run-evidence-verifier,
  .run-evidence-failed .run-evidence-subtitle {
    color: var(--thread-run-danger-text);
  }

  .run-evidence-warning .run-evidence-summary {
    border-color: var(--thread-run-evidence-warning-border);
  }

  .run-evidence-warning .run-evidence-verifier,
  .run-evidence-warning .run-evidence-subtitle {
    color: var(--thread-run-state-text);
  }

  .run-evidence-passed .run-evidence-summary {
    border-color: var(--thread-run-evidence-passed-border);
  }

  .run-evidence-passed .run-evidence-verifier,
  .run-evidence-passed .run-evidence-subtitle {
    color: var(--thread-run-success-text);
  }

  .run-evidence-body {
    display: grid;
    gap: 10px;
  }

  .run-evidence-meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(156px, 1fr));
    gap: 8px;
  }

  .run-evidence-meta-item,
  .run-evidence-step,
  .run-evidence-finding-group {
    display: grid;
    gap: 6px;
    padding: 9px 10px;
    border-radius: 8px;
    border: 1px solid var(--thread-run-evidence-surface-border);
    background: var(--thread-run-evidence-surface-background);
  }

  .run-evidence-meta-item-wide {
    grid-column: 1 / -1;
  }

  .run-evidence-meta-label {
    color: var(--thread-run-evidence-subtle-text);
  }

  .run-evidence-chip-row,
  .run-evidence-tool-row {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    min-width: 0;
  }

  .run-evidence-chip,
  .run-evidence-tool {
    max-width: 100%;
    overflow: hidden;
    padding: 3px 6px;
    border-radius: 6px;
    border: 1px solid var(--thread-run-evidence-chip-border);
    background: var(--thread-run-evidence-chip-background);
    color: var(--thread-run-evidence-muted-text);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-evidence-step-list {
    display: grid;
    gap: 6px;
  }

  .run-evidence-step {
    gap: 8px;
  }

  .run-evidence-step-main {
    min-width: 0;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
  }

  .run-evidence-step-count {
    flex-shrink: 0;
    color: var(--thread-run-evidence-muted-text);
    white-space: nowrap;
  }

  .run-evidence-tool {
    color: var(--thread-run-evidence-tool-text);
  }

  .run-evidence-findings {
    display: grid;
    gap: 8px;
  }

  .run-evidence-finding {
    color: var(--thread-run-evidence-finding-text);
    font-family: var(--thread-font-sans);
    font-size: 11px;
    line-height: 1.45;
  }

  .thread-thinking-state {
    display: grid;
    gap: 8px;
    box-sizing: border-box;
    padding-block: 2px;
    padding-inline-start: var(--thread-transient-rail-offset);
    border-radius: 0;
    border: 0;
    background: var(--thread-thinking-background);
  }

  .thread-thinking-header {
    display: flex;
    align-items: center;
    gap: 0;
    min-width: 0;
    color: var(--thread-thinking-text);
    font-family: var(--thread-font-sans);
    font-size: 13px;
    font-weight: 440;
    letter-spacing: 0;
    line-height: 1.4;
  }

  .thread-thinking-step-time,
  .thread-thinking-badge {
    font-family: var(--thread-font-mono);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .thread-thinking-badge {
    margin-left: auto;
    padding: 2px 6px;
    border-radius: 999px;
    background: var(--thread-thinking-badge-background);
    color: var(--thread-thinking-text);
  }

  .thread-thinking-steps {
    display: grid;
    gap: 4px;
  }

  .thread-thinking-step {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 8px;
    color: var(--thread-thinking-step-text);
    font-family: var(--thread-font-mono);
    font-size: 10px;
    line-height: 1.55;
  }

  .thread-thinking-step-time {
    color: var(--thread-thinking-step-time-text);
    white-space: nowrap;
  }

  .thread-composer-dock {
    position: relative;
    z-index: 6;
    padding: 0;
    box-sizing: border-box;
    margin-top: auto;
  }

  .thread-reply-dock-placeholder {
    display: grid;
    gap: 8px;
    padding: 16px 18px;
    border-radius: 24px;
    border: 1px solid var(--thread-reply-placeholder-border);
    background: var(--thread-reply-placeholder-background);
    box-shadow: var(--thread-reply-placeholder-shadow);
    backdrop-filter: blur(18px) saturate(1.04);
    -webkit-backdrop-filter: blur(18px) saturate(1.04);
  }

  .thread-reply-dock-placeholder-title {
    color: var(--thread-reply-placeholder-title);
    font-family: var(--thread-font-sans);
    font-size: 15px;
    line-height: 1.4;
  }

  .thread-reply-dock-placeholder-hint {
    color: var(--thread-reply-placeholder-hint);
    font-family: var(--thread-font-mono);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }


  @keyframes step-pulse {
    0%,
    100% {
      box-shadow: 0 0 0 rgba(141, 183, 255, 0);
    }

    50% {
      box-shadow: 0 0 0 1px rgba(141, 183, 255, 0.12);
    }
  }

  @keyframes thinking-dot-wave {
    0%,
    80%,
    100% {
      opacity: 0.24;
    }

    40% {
      opacity: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .run-step-segment-running,
    .thinking-status-dots span {
      animation: none;
    }
  }

  @media (max-width: 720px) {
    .thread-panel-header {
      padding:
        var(--thread-header-status-halo-clearance)
        0
        8px
        var(--thread-header-status-halo-clearance);
    }

    .run-compact-summary {
      align-items: start;
    }

    .run-compact-metrics {
      width: 100%;
      justify-content: flex-start;
    }

    .thread-header-title {
      font-size: clamp(15px, 5vw, 18px);
    }

    .thread-header-panel-toggle-group {
      gap: 6px;
    }

    .thread-content {
      padding: 10px 0 0;
    }

    .message-stack {
      gap: 14px;
      padding-bottom: 22px;
    }

    .thread-composer-dock {
      padding: 0;
    }
  }
</style>
