export const LOCAL_PREVIEW_MEMBER_PREFIX = '__cortex-preview-user__';
export const LOCAL_PREVIEW_IDEA_PREFIX = '__cortex-preview-idea__';
export const LOCAL_PREVIEW_TEXT_LAB_ID = `${LOCAL_PREVIEW_IDEA_PREFIX}illo-text-lab`;
export const LOCAL_PREVIEW_STORAGE_KEY = 'illo:cortex:local-preview';
export const LOCAL_PREVIEW_APP_KIND = 'local-preview-orbit-app';

export type LocalPreviewMember = {
  id: string;
  name: string;
  email: string;
  color: string;
};

export type LocalPreviewTeamMember = {
  id: string;
  name: string;
  color: string;
  email?: string;
};

export type LocalPreviewAnchorMember = LocalPreviewTeamMember & {
  __localPreviewAnchor?: true;
};

export type LocalPreviewGeneratedIdea = {
  id: string;
  title: string;
  display_title: string;
  description: string | null;
  status: string;
  origin: string;
  salience_score: number;
  position_x: number | null;
  position_y: number | null;
  created_at: string;
  updated_at: string;
  user_id: string;
  author_name: string;
  author_color: string;
  thread_count: number;
  active_agents: number;
  attachments: any[];
  archived_at: string | null;
};

export const LOCAL_PREVIEW_MEMBER_SEEDS = [
  { name: 'Maya', email: 'maya@example.test' },
  { name: 'Theo', email: 'theo@example.test' },
  { name: 'Iris', email: 'iris@example.test' },
] as const;

export const LOCAL_PREVIEW_APP_SEEDS = [
  { name: 'Orbit CRM', key: 'orbit-crm', accent: '#57cfa0', metric: '12', label: 'leads' },
  { name: 'Launch Board', key: 'launch-board', accent: '#8db7ff', metric: '04', label: 'tasks' },
  { name: 'Fit Notes', key: 'fit-notes', accent: '#57CFA0', metric: '27', label: 'notes' },
] as const;

interface LocalPreviewIdea {
  id: string;
  title?: string;
  display_title?: string;
  description?: string | null;
  status?: string;
  created_at?: string;
  updated_at?: string;
  user_id?: string;
  author_name?: string;
  author_color?: string;
}

interface LocalPreviewThreadMember {
  id?: string;
  name?: string;
  email?: string;
  color?: string;
}

interface LocalPreviewStreamItem {
  type: 'message' | 'run';
  timestamp: string;
  id: string;
  role?: string;
  content?: string;
  user_id?: string;
  user_name?: string;
  user_color?: string;
  metadata?: Record<string, any>;
  status?: string;
  title?: string;
  skill_name?: string;
  model_used?: string;
  thinking_used?: string;
  tokens_total?: number;
  duration_sec?: number;
  last_activity?: string;
  work_log?: { time?: string; text: string; kind?: string; tool_name?: string; status?: string }[];
  activity_trace?: { at?: string; activity: string; kind?: string; tool_name?: string; status?: string }[];
  tool_calls?: { tool: string; args?: string; at?: string; status?: string; finished_at?: string; result?: string }[];
  work_summary?: Record<string, any>;
  started_at?: string;
  completed_at?: string;
  execution_profile?: string;
  run_id?: number;
  idea_id?: string;
  thread_id?: string;
}

export function isLocalPreviewMemberId(id: unknown): boolean {
  return typeof id === 'string' && id.startsWith(LOCAL_PREVIEW_MEMBER_PREFIX);
}

export function isLocalPreviewIdeaId(id: unknown): boolean {
  return typeof id === 'string' && id.startsWith(LOCAL_PREVIEW_IDEA_PREFIX);
}

export function isLocalPreviewAnchorMember(member: unknown): boolean {
  return !!(member as LocalPreviewAnchorMember | null)?.__localPreviewAnchor;
}

export function clampLocalPreviewValue(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function isLocalPreviewDummyApp(app: { key?: string | null; metadata?: Record<string, any> | null } | null | undefined) {
  return app?.metadata?.local_preview_kind === LOCAL_PREVIEW_APP_KIND
    || String(app?.key || '').startsWith('local-preview-orbit-');
}

function normalizeLocalPreviewHexColor(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(trimmed)) return null;
  if (trimmed.length === 4) {
    return `#${trimmed[1]}${trimmed[1]}${trimmed[2]}${trimmed[2]}${trimmed[3]}${trimmed[3]}`;
  }
  return trimmed;
}

function channelToLinear(channel: number) {
  const normalized = channel / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

function previewCollaboratorColor(baseColor: string | null | undefined) {
  const normalized = normalizeLocalPreviewHexColor(baseColor) ?? '#c51f4a';
  const value = Number.parseInt(normalized.slice(1), 16);
  const r = (value >> 16) & 255;
  const g = (value >> 8) & 255;
  const b = value & 255;
  const warmBias = r - b;
  const luminance = 0.2126 * channelToLinear(r) + 0.7152 * channelToLinear(g) + 0.0722 * channelToLinear(b);

  if (warmBias > 18) return '#087f5b';
  if (warmBias < -18) return '#c51f4a';
  return luminance > 0.42 ? '#4c1d95' : '#6f8f00';
}

function previewMemberColor(baseColor: string | null | undefined, index: number) {
  const previewColorPool = ['#c51f4a', '#c026d3', '#087f5b', '#6f8f00'] as const;
  const primary = previewCollaboratorColor(baseColor);
  const base = normalizeLocalPreviewHexColor(baseColor);
  const remaining = previewColorPool.filter(
    (color) => color !== primary && color.toLowerCase() !== base?.toLowerCase(),
  );
  return [primary, ...remaining][index] ?? previewColorPool[index % previewColorPool.length];
}

export function buildLocalPreviewMembers(baseColor: string | null | undefined, count: number): LocalPreviewMember[] {
  return Array.from({ length: clampLocalPreviewValue(count, 0, LOCAL_PREVIEW_MEMBER_SEEDS.length) }, (_, index) => ({
    id: `${LOCAL_PREVIEW_MEMBER_PREFIX}${index}`,
    name: LOCAL_PREVIEW_MEMBER_SEEDS[index]?.name ?? `Preview ${index + 1}`,
    email: LOCAL_PREVIEW_MEMBER_SEEDS[index]?.email ?? `preview-${index + 1}@example.test`,
    color: previewMemberColor(baseColor, index),
  }));
}

export function buildLocalPreviewAnchorMember(user: {
  id?: string | null;
  name?: string | null;
  email?: string | null;
  color?: string | null;
} | null | undefined): LocalPreviewAnchorMember | null {
  if (!user?.id) return null;
  return {
    id: user.id,
    name: user.name || user.email || 'You',
    email: user.email ?? '',
    color: normalizeLocalPreviewHexColor(user.color) ?? '#c51f4a',
    __localPreviewAnchor: true,
  };
}

function previewIdeaStatus(memberIndex: number, blobIndex: number) {
  if (memberIndex === 0 && blobIndex === 0) return 'working';
  if (memberIndex === 1 && blobIndex === 0) return 'done';
  if (blobIndex === 2) return 'done';
  return 'idle';
}

function previewIdeaActiveAgents(memberIndex: number, blobIndex: number) {
  return memberIndex === 0 && blobIndex === 0 ? 2 : 0;
}

export function buildLocalPreviewIdeas(
  previewMembers: LocalPreviewMember[],
  blobsPerUser: number,
): LocalPreviewGeneratedIdea[] {
  const previewBlobTitles = [
    'Storefront refresh',
    'Launch notes',
    'Visual pass',
    'Orbit review',
    'Drop concept',
    'Window study',
  ] as const;
  const ideaCount = clampLocalPreviewValue(blobsPerUser, 0, 5);

  const previewIdeas: LocalPreviewGeneratedIdea[] = previewMembers.flatMap((member, memberIndex) =>
    Array.from({ length: ideaCount }, (_, blobIndex) => {
      const titleSeed = previewBlobTitles[(memberIndex * 2 + blobIndex) % previewBlobTitles.length];
      const createdAt = new Date(Date.now() - (memberIndex * 5 + blobIndex) * 60_000).toISOString();
      const previewStatus = previewIdeaStatus(memberIndex, blobIndex);
      const previewActiveAgents = previewIdeaActiveAgents(memberIndex, blobIndex);

      return {
        id: `${LOCAL_PREVIEW_IDEA_PREFIX}${memberIndex}-${blobIndex}`,
        title: titleSeed,
        display_title: titleSeed,
        description: 'Local preview blob for Cortex workspace tuning.',
        status: previewStatus,
        origin: 'local-preview',
        salience_score: Math.max(4, 7 - blobIndex),
        position_x: null,
        position_y: null,
        created_at: createdAt,
        updated_at: createdAt,
        user_id: member.id,
        author_name: member.name,
        author_color: member.color,
        thread_count: previewStatus === 'working' ? 2 : 1,
        active_agents: previewActiveAgents,
        attachments: [],
        archived_at: null,
      };
    }),
  );

  const textLabOwner = previewMembers[0];
  if (textLabOwner) {
    const createdAt = new Date(Date.now() - 90_000).toISOString();
    previewIdeas.unshift({
      id: LOCAL_PREVIEW_TEXT_LAB_ID,
      title: 'Illo text lab',
      display_title: 'Illo text lab',
      description: 'Dev-only thread for reviewing Illo message, reflection, tool, and live status typography.',
      status: 'working',
      origin: 'local-preview',
      salience_score: 9,
      position_x: null,
      position_y: null,
      created_at: createdAt,
      updated_at: createdAt,
      user_id: textLabOwner.id,
      author_name: textLabOwner.name,
      author_color: textLabOwner.color,
      thread_count: 3,
      active_agents: 1,
      attachments: [],
      archived_at: null,
    });
  }

  return previewIdeas;
}

export function previewMemberSignature(members: Array<{ id?: string; name?: string; email?: string; color?: string }>) {
  return JSON.stringify(
    members.map((member) => ({
      id: member.id,
      name: member.name,
      email: member.email,
      color: member.color,
    })),
  );
}

export function previewIdeaSignature(ideas: Array<{ id?: string; user_id?: string; title?: string; status?: string }>) {
  return JSON.stringify(
    ideas.map((idea) => ({
      id: idea.id,
      user_id: idea.user_id,
      title: idea.title,
      status: idea.status,
    })),
  );
}

export function stripLocalPreviewMembers<T extends { id?: string }>(members: T[]): T[] {
  return members.filter((member) => !isLocalPreviewMemberId(member?.id) && !isLocalPreviewAnchorMember(member));
}

export function stripLocalPreviewIdeas<T extends { id?: string }>(ideas: T[]): T[] {
  return ideas.filter((idea) => !isLocalPreviewIdeaId(idea?.id));
}

function hashString(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) >>> 0;
  }
  return hash;
}

function isoFrom(baseMs: number, offsetMs: number): string {
  return new Date(baseMs + offsetMs).toISOString();
}

function parseBaseTime(idea: LocalPreviewIdea): number {
  const parsed = new Date(idea.created_at || idea.updated_at || '').getTime();
  return Number.isFinite(parsed) ? parsed : Date.now() - 4 * 60_000;
}

function previewRunId(ideaId: string): number {
  return 930_000 + (hashString(ideaId) % 60_000);
}

function previewMemberForIdea(
  idea: LocalPreviewIdea,
  members: LocalPreviewThreadMember[],
): Required<Pick<LocalPreviewThreadMember, 'id' | 'name' | 'color'>> {
  const member = members.find((candidate) => candidate.id === idea.user_id);
  return {
    id: member?.id || idea.user_id || `${LOCAL_PREVIEW_MEMBER_PREFIX}0`,
    name: member?.name || idea.author_name || 'Maya',
    color: member?.color || idea.author_color || '#8db7ff',
  };
}

function buildLocalPreviewTextLabThreadStream(
  idea: LocalPreviewIdea,
  members: LocalPreviewThreadMember[] = [],
): LocalPreviewStreamItem[] {
  const member = previewMemberForIdea(idea, members);
  const baseMs = parseBaseTime(idea);
  const runId = previewRunId(idea.id);
  const promptAt = isoFrom(baseMs, 0);
  const answerAt = isoFrom(baseMs, 9_000);
  const runStartedAt = isoFrom(baseMs, 19_000);
  const reflectAt = isoFrom(baseMs, 29_000);
  const thoughtAt = isoFrom(baseMs, 39_000);
  const readAt = isoFrom(baseMs, 49_000);
  const readDoneAt = isoFrom(baseMs, 58_000);
  const execProseAt = isoFrom(baseMs, 64_000);
  const execAt = isoFrom(baseMs, 70_000);
  const patchAt = isoFrom(baseMs, 84_000);
  const patchDoneAt = isoFrom(baseMs, 96_000);
  const writingAt = isoFrom(baseMs, 108_000);
  const statusAt = isoFrom(baseMs, 121_000);
  const targetFile = 'frontend/src/lib/features/threads/components/ThreadTranscript.svelte';

  const workLog: NonNullable<LocalPreviewStreamItem['work_log']> = [
    { time: runStartedAt, text: 'Started', kind: 'run.started' },
    {
      time: reflectAt,
      text: '**Reviewing reflection typography** The reflection should read as a quiet thought, without a visible system label.',
      kind: 'run.activity',
    },
    {
      time: thoughtAt,
      text: 'Checking how normal live prose wraps when it gets long enough to take a second line in the transcript.',
      kind: 'run.step_started',
    },
    { time: readAt, text: 'Using read_file', kind: 'run.tool_started', tool_name: 'read_file', status: 'running' },
    { time: readDoneAt, text: 'read_file completed', kind: 'run.tool_completed', tool_name: 'read_file', status: 'completed' },
    {
      time: execProseAt,
      text: 'Using exec_command: npm run check',
      kind: 'run.activity',
      tool_name: 'exec_command',
      status: 'running',
    },
    { time: execAt, text: 'Using exec_command', kind: 'run.tool_started', tool_name: 'exec_command', status: 'running' },
    { time: patchAt, text: 'Using apply_patch', kind: 'run.tool_started', tool_name: 'apply_patch', status: 'running' },
    { time: patchDoneAt, text: 'apply_patch completed', kind: 'run.tool_completed', tool_name: 'apply_patch', status: 'completed' },
    { time: writingAt, text: 'Writing response... (~244 output tokens)', kind: 'run.activity' },
    { time: statusAt, text: 'Thinking through the final polish... (1m, ~120 internal tokens)', kind: 'run.activity' },
  ];

  const toolCalls: NonNullable<LocalPreviewStreamItem['tool_calls']> = [
    {
      tool: 'read_file',
      args: JSON.stringify({ path: targetFile }),
      at: readAt,
      status: 'completed',
      finished_at: readDoneAt,
      result: 'loaded current thread typography styles',
    },
    {
      tool: 'exec_command',
      args: JSON.stringify({ command: 'npm run check' }),
      at: execAt,
      status: 'running',
    },
    {
      tool: 'apply_patch',
      args: JSON.stringify({ path: targetFile }),
      at: patchAt,
      status: 'completed',
      finished_at: patchDoneAt,
      result: 'softened activity stream text',
    },
  ];

  return [
    {
      type: 'message',
      timestamp: promptAt,
      id: `${idea.id}:prompt`,
      role: 'user',
      user_id: member.id,
      user_name: member.name,
      user_color: member.color,
      content: 'Can you show me every kind of Illo text in one thread so I can review the typography?',
      metadata: { local_preview: true, text_lab: true },
    },
    {
      type: 'message',
      timestamp: answerAt,
      id: `${idea.id}:illo-markdown`,
      role: 'illo',
      content: [
        '**Short answer:** yes. This thread is a dev-only typography lab for Illo output.',
        '',
        'It includes normal prose, *quiet emphasis*, inline `code`, a compact list, and a quote.',
        '',
        '- Plain assistant answer text',
        '- Markdown emphasis and list rhythm',
        '- Reflection snippets and live work rows',
        '',
        '> Quoted text should feel calm, not like an alert.',
      ].join('\n'),
      metadata: { local_preview: true, text_lab: true },
    },
    {
      type: 'message',
      timestamp: isoFrom(baseMs, 14_000),
      id: `${idea.id}:illo-code`,
      role: 'illo',
      content: [
        'A tiny code block should stay readable inside an Illo message:',
        '',
        '```ts',
        "const status = 'Thinking...';",
        'render(status);',
        '```',
      ].join('\n'),
      metadata: { local_preview: true, text_lab: true },
    },
    {
      type: 'run',
      timestamp: runStartedAt,
      id: String(runId),
      run_id: runId,
      idea_id: idea.id,
      thread_id: idea.id,
      title: 'Illo text style lab',
      status: 'running',
      started_at: runStartedAt,
      model_used: 'preview-model',
      thinking_used: 'high',
      tokens_total: 3420,
      execution_profile: 'fast',
      last_activity: 'Writing response... (~244 output tokens)',
      work_log: workLog,
      activity_trace: workLog.map((entry) => activityEntry(
        entry.time ?? runStartedAt,
        entry.text,
        entry.kind ?? 'run.activity',
        entry.tool_name,
        entry.status,
      )),
      tool_calls: toolCalls,
      work_summary: {
        status: 'running',
        tool_count: toolCalls.length,
        activity_count: workLog.length,
        tool_names: toolCalls.map((toolCall) => toolCall.tool),
      },
      metadata: { local_preview: true, text_lab: true },
    },
  ];
}

function activityEntry(time: string, text: string, kind: string, toolName?: string, status?: string) {
  return {
    at: time,
    activity: text,
    kind,
    tool_name: toolName,
    status,
  };
}

export function buildLocalPreviewThreadStream(
  idea: LocalPreviewIdea,
  members: LocalPreviewThreadMember[] = [],
): LocalPreviewStreamItem[] {
  if (idea.id === LOCAL_PREVIEW_TEXT_LAB_ID) {
    return buildLocalPreviewTextLabThreadStream(idea, members);
  }

  const title = idea.display_title || idea.title || 'Preview thread';
  const member = previewMemberForIdea(idea, members);
  const baseMs = parseBaseTime(idea);
  const runId = previewRunId(idea.id);
  const isWorking = idea.status === 'working';
  const startedAt = isoFrom(baseMs, 24_000);
  const readAt = isoFrom(baseMs, 34_000);
  const thoughtAt = isoFrom(baseMs, 45_000);
  const editAt = isoFrom(baseMs, 58_000);
  const editDoneAt = isoFrom(baseMs, 79_000);
  const checkAt = isoFrom(baseMs, 92_000);
  const completedAt = isWorking ? undefined : isoFrom(baseMs, 131_000);
  const runStatus = isWorking ? 'running' : 'completed';
  const targetFile = 'frontend/src/lib/features/threads/components/ThreadTranscript.svelte';
  const workLog = [
    { time: startedAt, text: 'Started', kind: 'run.started' },
    { time: readAt, text: `Reading ${title} context`, kind: 'run.activity' },
    { time: thoughtAt, text: 'Thinking through the smallest timeline surface that still shows real work.', kind: 'run.step_started' },
    { time: editAt, text: 'Using edit_file', kind: 'run.tool_started' },
    { time: editDoneAt, text: 'edit_file completed', kind: 'run.tool_completed' },
    { time: checkAt, text: 'Using exec_command', kind: 'run.tool_started' },
    ...(isWorking
      ? []
      : [
          { time: isoFrom(baseMs, 112_000), text: 'exec_command completed', kind: 'run.tool_completed' },
          { time: completedAt!, text: 'Completed', kind: 'run.completed' },
        ]),
  ];
  const toolCalls = [
    {
      tool: 'edit_file',
      args: JSON.stringify({ path: targetFile }),
      at: editAt,
      status: 'completed',
      finished_at: editDoneAt,
      result: 'updated thread timeline renderer',
    },
    {
      tool: 'exec_command',
      args: JSON.stringify({ cmd: 'npm run check' }),
      at: checkAt,
      status: isWorking ? 'running' : 'completed',
      finished_at: isWorking ? undefined : isoFrom(baseMs, 112_000),
      result: isWorking ? undefined : 'svelte-check found 0 errors',
    },
  ];
  const activityTrace = workLog.map((entry) => activityEntry(
    entry.time,
    entry.text,
    entry.kind,
    entry.kind.includes('tool') ? entry.text.replace(/^Using\s+/, '').replace(/\s+(completed|failed)$/, '') : undefined,
    entry.kind.endsWith('completed') ? 'completed' : entry.kind.endsWith('started') ? 'running' : undefined,
  ));

  const items: LocalPreviewStreamItem[] = [
    {
      type: 'message',
      timestamp: isoFrom(baseMs, 0),
      id: `${idea.id}:prompt`,
      role: 'user',
      user_id: member.id,
      user_name: member.name,
      user_color: member.color,
      content: `Can we make the "${title}" thread show thoughts and tools in chronological order? I want the completed state to collapse into a quiet Worked for line.`,
      metadata: { local_preview: true },
    },
    {
      type: 'message',
      timestamp: isoFrom(baseMs, 12_000),
      id: `${idea.id}:ack`,
      role: 'illo',
      content: 'Absolutely. I will keep the stream lightweight while preserving enough detail to review the timeline behavior.',
      metadata: { local_preview: true },
    },
    {
      type: 'run',
      timestamp: startedAt,
      id: String(runId),
      run_id: runId,
      idea_id: idea.id,
      thread_id: idea.id,
      title: 'Thread timeline preview',
      status: runStatus,
      started_at: startedAt,
      completed_at: completedAt,
      duration_sec: isWorking ? undefined : 107,
      model_used: 'preview-model',
      thinking_used: 'medium',
      tokens_total: isWorking ? 1840 : 2412,
      execution_profile: 'fast',
      last_activity: isWorking ? 'Using exec_command' : 'Completed',
      work_log: workLog,
      activity_trace: activityTrace,
      tool_calls: toolCalls,
      work_summary: {
        status: runStatus,
        duration_sec: isWorking ? undefined : 107,
        tool_count: toolCalls.length,
        activity_count: workLog.length,
        tool_names: ['edit_file', 'exec_command'],
      },
      metadata: { local_preview: true },
    },
  ];

  if (!isWorking) {
    items.push({
      type: 'message',
      timestamp: isoFrom(baseMs, 146_000),
      id: `${idea.id}:final`,
      role: 'illo',
      content: 'Done. The work log now reads as an ordered timeline while the finished run stays tucked under the minimal Worked for summary.',
      metadata: { local_preview: true, run_id: runId },
    });
  }

  return items;
}
