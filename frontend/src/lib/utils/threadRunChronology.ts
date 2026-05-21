export type ThreadRunChronologyWorkItem = {
  kind?: string;
  at?: string;
  time?: string;
  text?: string;
};

export type ThreadRunChronologyLiveTextItem = {
  id?: string | number | null;
  timestamp?: string;
  metadata?: Record<string, any> | null;
};

export type ThreadRunChronologySegment<TWork, TLive> =
  | {
      kind: 'work';
      items: TWork[];
      showLiveCue: boolean;
    }
  | {
      kind: 'live_text';
      item: TLive;
    };

function parseTimeMs(value: unknown): number | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function metadataFor(item: ThreadRunChronologyLiveTextItem): Record<string, any> {
  return item.metadata && typeof item.metadata === 'object' ? item.metadata : {};
}

function liveTextStartMs(item: ThreadRunChronologyLiveTextItem): number | null {
  const metadata = metadataFor(item);
  return parseTimeMs(metadata.live_agent_text_first_delta_at) ?? parseTimeMs(item.timestamp);
}

function workItemTimeMs(item: ThreadRunChronologyWorkItem): number | null {
  return parseTimeMs(item.at) ?? parseTimeMs(item.time);
}

function normalizedText(value: unknown): string {
  return String(value ?? '')
    .toLowerCase()
    .replace(/[`*_#>\[\]()]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function isLiveResponseStatus(item: ThreadRunChronologyWorkItem): boolean {
  if (item.kind !== 'thought') return false;
  const text = normalizedText(item.text);
  return text.startsWith('writing response') || text.startsWith('streaming');
}

function compareNullableTimes(left: number | null, right: number | null): number {
  if (left !== null && right !== null && left !== right) return left - right;
  if (left !== null && right === null) return -1;
  if (left === null && right !== null) return 1;
  return 0;
}

function workBelongsBeforeLiveText(
  work: { ms: number | null; index: number },
  live: { ms: number | null; index: number },
): boolean {
  const timeOrder = compareNullableTimes(work.ms, live.ms);
  return timeOrder < 0 || (timeOrder === 0 && work.index < live.index);
}

export function buildChronologicalRunSegments<
  TWork extends ThreadRunChronologyWorkItem,
  TLive extends ThreadRunChronologyLiveTextItem,
>(
  workItems: readonly TWork[],
  liveTextItems: readonly TLive[],
  {
    includeTrailingCue = false,
    suppressLiveResponseStatus = true,
  }: {
    includeTrailingCue?: boolean;
    suppressLiveResponseStatus?: boolean;
  } = {},
): ThreadRunChronologySegment<TWork, TLive>[] {
  const liveRecords = liveTextItems
    .map((item, index) => ({ item, index, ms: liveTextStartMs(item) }))
    .sort((left, right) => compareNullableTimes(left.ms, right.ms) || left.index - right.index);
  const hasLiveText = liveRecords.length > 0;

  const filteredWorkItems = suppressLiveResponseStatus && hasLiveText
    ? workItems.filter((item) => !isLiveResponseStatus(item))
    : [...workItems];

  const workRecords = filteredWorkItems.map((item, index) => ({
    item,
    index,
    ms: workItemTimeMs(item),
  }));

  if (!hasLiveText) {
    if (workRecords.length === 0 && !includeTrailingCue) return [];
    return [{
      kind: 'work',
      items: workRecords.map((record) => record.item),
      showLiveCue: includeTrailingCue,
    }];
  }

  const segments: ThreadRunChronologySegment<TWork, TLive>[] = [];
  let workCursor = 0;
  const pushWorkSegment = (items: TWork[], showLiveCue: boolean) => {
    if (items.length === 0 && !showLiveCue) return;
    segments.push({ kind: 'work', items, showLiveCue });
  };

  for (const live of liveRecords) {
    const segmentWork: TWork[] = [];
    while (
      workCursor < workRecords.length
      && workBelongsBeforeLiveText(workRecords[workCursor], live)
    ) {
      segmentWork.push(workRecords[workCursor].item);
      workCursor += 1;
    }
    pushWorkSegment(segmentWork, false);
    segments.push({ kind: 'live_text', item: live.item });
  }

  const tailWork = workRecords.slice(workCursor).map((record) => record.item);
  pushWorkSegment(tailWork, includeTrailingCue);

  return segments;
}
