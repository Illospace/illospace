export type ThreadStreamOrderingItem = {
  type?: string;
  id?: string | number | null;
  role?: string | null;
  content?: string | null;
  run_id?: string | number | null;
  metadata?: Record<string, any> | null;
};

function keyFor(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null;
  return String(value);
}

function metadataFor(item: ThreadStreamOrderingItem): Record<string, any> {
  return item.metadata && typeof item.metadata === 'object' ? item.metadata : {};
}

function queuedAfterRunId(item: ThreadStreamOrderingItem): string | null {
  const metadata = metadataFor(item);
  return keyFor(metadata.queued_after_run_id);
}

function itemRunId(item: ThreadStreamOrderingItem): string | null {
  const metadata = metadataFor(item);
  return keyFor(item.run_id ?? metadata.run_id ?? (item.type === 'run' ? item.id : null));
}

function isAgentRunReply(item: ThreadStreamOrderingItem): boolean {
  const role = item.role === 'assistant' || item.role === 'illo';
  if (item.type !== 'message' || !role) return false;
  if (metadataFor(item).live_agent_text) return false;
  return Boolean(String(item.content || '').trim() && itemRunId(item));
}

function isPreToolLiveAgentText(item: ThreadStreamOrderingItem): boolean {
  const metadata = metadataFor(item);
  const role = item.role === 'assistant' || item.role === 'illo';
  return (
    item.type === 'message'
    && role
    && Boolean(metadata.live_agent_text)
    && metadata.live_agent_text_after_tool !== true
    && Boolean(itemRunId(item))
  );
}

function orderPreToolLiveTextBeforeRuns<T extends ThreadStreamOrderingItem>(items: readonly T[]): T[] {
  const runIds = new Set<string>();
  for (const item of items) {
    if (item.type !== 'run') continue;
    const runId = itemRunId(item);
    if (runId) runIds.add(runId);
  }
  if (runIds.size === 0) return [...items];

  const liveTextByRun = new Map<string, T[]>();
  const movedLiveText = new Set<T>();
  for (const item of items) {
    const runId = itemRunId(item);
    if (!runId || !runIds.has(runId) || !isPreToolLiveAgentText(item)) continue;
    const runItems = liveTextByRun.get(runId);
    if (runItems) runItems.push(item);
    else liveTextByRun.set(runId, [item]);
    movedLiveText.add(item);
  }
  if (movedLiveText.size === 0) return [...items];

  const flushedRunIds = new Set<string>();
  const ordered: T[] = [];
  const flushLiveText = (runId: string) => {
    if (flushedRunIds.has(runId)) return;
    const liveText = liveTextByRun.get(runId);
    if (liveText?.length) ordered.push(...liveText);
    flushedRunIds.add(runId);
  };

  for (const item of items) {
    if (movedLiveText.has(item)) continue;
    const runId = item.type === 'run' ? itemRunId(item) : null;
    if (runId) flushLiveText(runId);
    ordered.push(item);
  }

  return ordered;
}

export function orderQueuedThreadStreamItems<T extends ThreadStreamOrderingItem>(items: readonly T[]): T[] {
  const orderedItems = orderPreToolLiveTextBeforeRuns(items);
  const blocksByAnchor = new Map<string, T[]>();
  const anchorIds = new Set<string>();

  for (const item of orderedItems) {
    const anchorId = queuedAfterRunId(item);
    if (!anchorId) continue;
    anchorIds.add(anchorId);
    const block = blocksByAnchor.get(anchorId);
    if (block) block.push(item);
    else blocksByAnchor.set(anchorId, [item]);
  }

  if (blocksByAnchor.size === 0) return orderedItems;

  const lastRunReplyIndexByAnchor = new Map<string, number>();
  const anchorsWithTarget = new Set<string>();
  for (let index = 0; index < orderedItems.length; index += 1) {
    const item = orderedItems[index];
    const runId = itemRunId(item);
    if (!runId || !anchorIds.has(runId)) continue;
    anchorsWithTarget.add(runId);
    if (isAgentRunReply(item)) lastRunReplyIndexByAnchor.set(runId, index);
  }

  const movedAnchors = new Set(
    [...anchorIds].filter((anchorId) => anchorsWithTarget.has(anchorId)),
  );
  if (movedAnchors.size === 0) return orderedItems;

  const flushedAnchors = new Set<string>();
  const flush = (anchorId: string, output: T[]) => {
    if (flushedAnchors.has(anchorId)) return;
    const block = blocksByAnchor.get(anchorId);
    if (!block?.length) return;
    output.push(...block);
    flushedAnchors.add(anchorId);
  };

  const ordered: T[] = [];
  for (let index = 0; index < orderedItems.length; index += 1) {
    const item = orderedItems[index];
    const anchorId = queuedAfterRunId(item);
    if (anchorId && movedAnchors.has(anchorId)) continue;

    ordered.push(item);

    const runId = itemRunId(item);
    if (!runId || !movedAnchors.has(runId)) continue;
    if (
      (isAgentRunReply(item) && lastRunReplyIndexByAnchor.get(runId) === index)
      || (item.type === 'run' && !lastRunReplyIndexByAnchor.has(runId))
    ) {
      flush(runId, ordered);
    }
  }

  for (const anchorId of movedAnchors) {
    flush(anchorId, ordered);
  }

  return ordered;
}
