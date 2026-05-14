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

export function orderQueuedThreadStreamItems<T extends ThreadStreamOrderingItem>(items: readonly T[]): T[] {
  const blocksByAnchor = new Map<string, T[]>();
  const anchorIds = new Set<string>();

  for (const item of items) {
    const anchorId = queuedAfterRunId(item);
    if (!anchorId) continue;
    anchorIds.add(anchorId);
    blocksByAnchor.set(anchorId, [...(blocksByAnchor.get(anchorId) ?? []), item]);
  }

  if (blocksByAnchor.size === 0) return [...items];

  const lastRunReplyIndexByAnchor = new Map<string, number>();
  const anchorsWithTarget = new Set<string>();
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    const runId = itemRunId(item);
    if (!runId || !anchorIds.has(runId)) continue;
    anchorsWithTarget.add(runId);
    if (isAgentRunReply(item)) lastRunReplyIndexByAnchor.set(runId, index);
  }

  const movedAnchors = new Set(
    [...anchorIds].filter((anchorId) => anchorsWithTarget.has(anchorId)),
  );
  if (movedAnchors.size === 0) return [...items];

  const flushedAnchors = new Set<string>();
  const flush = (anchorId: string, output: T[]) => {
    if (flushedAnchors.has(anchorId)) return;
    const block = blocksByAnchor.get(anchorId);
    if (!block?.length) return;
    output.push(...block);
    flushedAnchors.add(anchorId);
  };

  const ordered: T[] = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
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
