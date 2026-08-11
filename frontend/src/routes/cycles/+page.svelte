<script lang="ts">
  import { dev } from '$app/environment';
  import { page } from '$app/stores';
  import { getContext, onMount } from 'svelte';

  import {
    api,
    type CyclePolicyConfigurationRead,
    type CyclePolicyFieldSourceRead,
    type CyclePolicyHistoryRead,
    type CycleRead,
    type CycleRunRead,
    type EffectiveCyclePolicyRead,
  } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationEmptyState,
    ConstellationIcon,
    ConstellationNotice,
    ConstellationPageFrame,
    ConstellationPill,
    ConstellationSearchField,
    ConstellationSegmentedToggle,
    ConstellationSkeletonBlock,
  } from '$lib/components/constellation';
  import {
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
    type ConstellationPageFrameModalContext,
  } from '$lib/components/constellation/constellationPageFrameContext';
  import AiPromptComposer from '$lib/features/composer/components/AiPromptComposer.svelte';
  import EffectiveCyclePolicyView from '$lib/features/cycles/components/EffectiveCyclePolicyView.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { parseServerDate, relativeTimeAgo } from '$lib/utils/datetime';

  type ThinkingLevel = '' | 'none' | 'low' | 'medium' | 'high' | 'xhigh';
  type ScheduleCadence = 'once' | 'daily' | 'weekdays' | 'weekly' | 'monthly' | 'custom';
  type FilterMode = 'all' | 'active';
  type PillTone = 'muted' | 'warning' | 'success' | 'danger' | 'info';

  type CycleForm = {
    name: string;
    prompt: string;
    schedule_expr: string;
    cadence: ScheduleCadence;
    date: string;
    time: string;
    weekday: string;
    monthday: string;
    custom_schedule: string;
    timezone: string;
    enabled: boolean;
    model_override: string;
    thinking_override: ThinkingLevel;
    target_idea_id: string;
  };

  type ParsedSchedule = Pick<
    CycleForm,
    'cadence' | 'date' | 'time' | 'weekday' | 'monthday' | 'custom_schedule'
  >;

  const THINKING_OPTIONS: Array<{ value: ThinkingLevel; label: string }> = [
    { value: '', label: 'Default' },
    { value: 'none', label: 'None' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'xhigh', label: 'XHigh' },
  ];

  const CADENCE_OPTIONS: Array<{ value: ScheduleCadence; label: string; description: string }> = [
    { value: 'once', label: 'Once', description: 'Single reminder' },
    { value: 'daily', label: 'Daily', description: 'Every day' },
    { value: 'weekdays', label: 'Weekdays', description: 'Monday to Friday' },
    { value: 'weekly', label: 'Weekly', description: 'One day each week' },
    { value: 'monthly', label: 'Monthly', description: 'One day each month' },
    { value: 'custom', label: 'Custom', description: 'Advanced cron' },
  ];
  const FILTER_OPTIONS: Array<{ key: FilterMode; label: string }> = [
    { key: 'all', label: 'All' },
    { key: 'active', label: 'Active' },
  ];

  const WEEKDAY_OPTIONS = [
    { value: '0', label: 'Sunday', plural: 'Sundays' },
    { value: '1', label: 'Monday', plural: 'Mondays' },
    { value: '2', label: 'Tuesday', plural: 'Tuesdays' },
    { value: '3', label: 'Wednesday', plural: 'Wednesdays' },
    { value: '4', label: 'Thursday', plural: 'Thursdays' },
    { value: '5', label: 'Friday', plural: 'Fridays' },
    { value: '6', label: 'Saturday', plural: 'Saturdays' },
  ];

  const MONTHDAY_OPTIONS = Array.from({ length: 31 }, (_, index) => String(index + 1));
  const DEFAULT_SCHEDULE = '0 9 * * *';
  const DEFAULT_TIME = '09:00';
  const ONE_TIME_PREFIX = 'at:';

  let cycles = $state<CycleRead[]>([]);
  let runs = $state<CycleRunRead[]>([]);
  let loading = $state(true);
  let loadError = $state('');
  let saving = $state(false);
  let deleting = $state(false);
  let selectedCycleId = $state<number | null>(null);
  let selectedRowId = $state<string | null>(null);
  let runsLoading = $state(false);
  let advancedOpen = $state(false);
  let search = $state('');
  let filterMode = $state<FilterMode>('all');
  let showCreateModal = $state(false);
  let previewRuns = $state<Record<number, CycleRunRead[]>>({});
  let previewPolicies = $state<Record<number, EffectiveCyclePolicyRead>>({});
  let previewPolicyHistories = $state<Record<number, CyclePolicyHistoryRead>>({});
  let behaviorPolicyRefreshSerial = $state(0);

  const workspacePageModalContext = getContext<ConstellationPageFrameModalContext | undefined>(
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
  );

  $effect(() => {
    return workspacePageModalContext?.registerRefreshAction({
      label: 'Refresh cycles',
      onclick: () => loadCycles(selectedCycleId),
    });
  });

  const defaultTimezone = () =>
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

  const localTimezone = defaultTimezone();

  const emptyForm = (): CycleForm => ({
    name: '',
    prompt: '',
    schedule_expr: DEFAULT_SCHEDULE,
    cadence: 'daily',
    date: defaultRunDate(),
    time: DEFAULT_TIME,
    weekday: '1',
    monthday: '1',
    custom_schedule: DEFAULT_SCHEDULE,
    timezone: localTimezone,
    enabled: true,
    model_override: '',
    thinking_override: '',
    target_idea_id: '',
  });

  let form = $state<CycleForm>(emptyForm());

  const selectedCycle = $derived.by(
    () => cycles.find((cycle) => cycle.id === selectedCycleId) ?? null,
  );
  const isCyclesPreview = $derived(dev && $page.url.searchParams.get('preview') === '1');
  const filteredCycles = $derived.by(() => {
    const needle = search.trim().toLowerCase();
    return cycles.filter((cycle) => {
      if (filterMode === 'active' && !cycle.enabled) return false;
      if (!needle) return true;
      return cycleSearchText(cycle).includes(needle);
    });
  });
  const selectedRunThreadId = $derived.by(
    () => runs.find((run) => Boolean(run.idea_id))?.idea_id ?? null,
  );
  const selectedThreadId = $derived.by(
    () => selectedCycle?.target_idea_id || selectedRunThreadId,
  );
  const schedulePreview = $derived.by(() => scheduleLabelForForm(form));

  function toneForStatus(status: string | null | undefined): PillTone {
    const normalized = String(status ?? '').toLowerCase();
    if (['completed', 'idle', 'success'].includes(normalized)) return 'success';
    if (['failed', 'error'].includes(normalized)) return 'danger';
    if (['running', 'queued', 'pending'].includes(normalized)) return 'warning';
    if (['skipped'].includes(normalized)) return 'info';
    return 'muted';
  }

  function cycleRowId(cycle: CycleRead): string {
    return `cycle:${cycle.id}`;
  }

  function cycleNeedsAttention(cycle: CycleRead): boolean {
    const status = String(cycle.last_status ?? '').toLowerCase();
    return Boolean(
      cycle.last_error ||
        ['failed', 'error'].includes(status) ||
        (cycle.enabled && !cycle.next_run_at),
    );
  }

  function cycleSearchText(cycle: CycleRead): string {
    return [
      cycle.name,
      cycle.prompt,
      cycle.schedule_expr,
      cycle.schedule_human,
      cycle.timezone,
      cycle.last_status,
      cycle.last_error,
      scheduleLabelForCycle(cycle),
      threadLabel(cycle),
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
  }

  function cycleStatusVariant(cycle: CycleRead): PillTone {
    if (cycleNeedsAttention(cycle)) return 'danger';
    if (!cycle.enabled) return 'muted';
    return toneForStatus(cycle.last_status) === 'warning' ? 'warning' : 'success';
  }

  function cycleStatusLabel(cycle: CycleRead): string {
    if (cycleNeedsAttention(cycle)) return 'Needs Work';
    if (!cycle.enabled) return 'Paused';
    if (cycle.last_status && ['running', 'queued', 'pending'].includes(cycle.last_status.toLowerCase())) {
      return sentenceCase(cycle.last_status);
    }
    return 'Active';
  }

  function cycleStatusDetail(cycle: CycleRead): string {
    const isOnce = isOneTimeSchedule(cycle.schedule_expr);
    if (cycle.last_error) return 'Latest run failed';
    if (cycle.next_run_at) return `${isOnce ? 'Runs' : 'Next'} ${formatDateTime(cycle.next_run_at)}`;
    if (isOnce && cycle.last_run_at) return `Ran ${timeAgo(cycle.last_run_at)}`;
    if (cycle.last_run_at) return `Last ${timeAgo(cycle.last_run_at)}`;
    return cycle.enabled ? 'No next run' : 'Paused schedule';
  }

  function cycleFactSummary(cycle: CycleRead): string {
    const cadence = parseSchedule(cycle.schedule_expr).cadence;
    const state = cadence === 'once' && !cycle.enabled && cycle.last_run_at ? 'done' : cycle.enabled ? 'active' : 'paused';
    return `${cadence} / ${state} / ${threadLabel(cycle).toLowerCase()}`;
  }

  function setFilter(key: string) {
    if (key === 'all' || key === 'active') {
      filterMode = key;
    }
  }

  function previewIso(daysOffset: number, hoursOffset = 0): string {
    return new Date(Date.now() + daysOffset * 86400000 + hoursOffset * 3600000).toISOString();
  }

  function previewNextRunAtForSchedule(scheduleExpr: string, enabled = true): string | null {
    if (!enabled) return null;
    const onceDate = oneTimeDateFromExpression(scheduleExpr);
    if (onceDate) return onceDate.toISOString();
    return previewIso(1);
  }

  function previewCycle(
    id: number,
    overrides: Partial<CycleRead> & Pick<CycleRead, 'name' | 'prompt' | 'schedule_expr'>,
  ): CycleRead {
    return {
      id,
      user_id: 'preview-user',
      org_id: 'preview-org',
      name: overrides.name,
      prompt: overrides.prompt,
      schedule_expr: overrides.schedule_expr,
      schedule_human: overrides.schedule_human ?? overrides.schedule_expr,
      timezone: overrides.timezone ?? localTimezone,
      enabled: overrides.enabled ?? true,
      model_override: overrides.model_override !== undefined ? overrides.model_override : null,
      thinking_override: overrides.thinking_override !== undefined ? overrides.thinking_override : null,
      execution_policy_key:
        overrides.execution_policy_key !== undefined ? overrides.execution_policy_key : null,
      execution_mode: 'reuse_same_idea',
      target_idea_id:
        overrides.target_idea_id !== undefined ? overrides.target_idea_id : `preview-cycle-${id}`,
      reopen_archived: overrides.reopen_archived ?? true,
      next_run_at: overrides.next_run_at !== undefined ? overrides.next_run_at : previewIso(1),
      last_run_at: overrides.last_run_at !== undefined ? overrides.last_run_at : previewIso(-1),
      last_status: overrides.last_status !== undefined ? overrides.last_status : 'completed',
      last_error: overrides.last_error !== undefined ? overrides.last_error : null,
      created_at: overrides.created_at ?? previewIso(-18),
      updated_at: overrides.updated_at ?? previewIso(-2),
    };
  }

  function previewRun(
    id: number,
    cycleId: number,
    status: string,
    prompt: string,
    daysOffset: number,
  ): CycleRunRead {
    const timestamp = previewIso(daysOffset);
    return {
      id,
      cycle_id: cycleId,
      scheduled_for: timestamp,
      started_at: timestamp,
      completed_at: status === 'running' ? null : previewIso(daysOffset, 1),
      status,
      error: status === 'failed' ? 'Preview run could not resolve the requested source.' : null,
      skip_reason: null,
      idea_id: `preview-run-${id}`,
      run_id: 8000 + id,
      prompt_snapshot: prompt,
      created_at: timestamp,
    };
  }

  function previewBehaviorPolicy(cycle: CycleRead): {
    policy: EffectiveCyclePolicyRead;
    history: CyclePolicyHistoryRead;
  } {
    const changedAt = previewIso(-2);
    const configuration: CyclePolicyConfigurationRead = {
      name: cycle.name,
      prompt: cycle.prompt,
      schedule_expr: cycle.schedule_expr,
      schedule_human: cycle.schedule_human,
      timezone: cycle.timezone,
      enabled: cycle.enabled,
      max_concurrency: 1,
      timeout_seconds: null,
      retry_policy: { max_attempts: 2 },
      model_override: cycle.model_override,
      thinking_override: cycle.thinking_override,
      execution_policy_key: cycle.execution_policy_key,
      target_idea_id: cycle.target_idea_id,
    };
    const activeGuidance = [
      'Use the current workspace state as the source of truth.',
      'Keep the result concise and name any blocker that needs attention.',
    ];
    const source: CyclePolicyFieldSourceRead = {
      version: 2,
      cycle_revision_id: 4100 + cycle.id,
      actor_type: 'human',
      actor_id: 'preview-user',
      source_reference: `api:/cycles/${cycle.id}/behavior-policy`,
      rationale: 'Approved behavior for the next run.',
      changed_at: changedAt,
      change_id: 5100 + cycle.id,
    };
    const fieldSources = Object.fromEntries(
      [...Object.keys(configuration), 'guidance'].map((field) => [field, { ...source }]),
    ) as Record<string, CyclePolicyFieldSourceRead>;
    const policy: EffectiveCyclePolicyRead = {
      workspace_id: 'preview-org',
      policy_kind: 'cycle',
      target_type: 'cycle',
      target_id: String(cycle.id),
      version: 2,
      revision_id: source.cycle_revision_id,
      configuration,
      guidance: activeGuidance,
      editable_fields: [
        'prompt',
        'schedule_expr',
        'timezone',
        'enabled',
        'model_override',
        'thinking_override',
        'guidance',
      ],
      output_targets: [
        {
          id: 6100 + cycle.id,
          target_type: 'cycle_ledger',
          target_id: String(cycle.id),
          label: 'Cycle ledger',
          config: { format: 'summary' },
          source_type: 'system',
          source_id: 'cycle-defaults',
          rationale: 'Keep a durable result for later review.',
          created_at: previewIso(-18),
          updated_at: changedAt,
        },
      ],
      output_targets_read_only: true,
      source: {
        revision_id: source.cycle_revision_id,
        actor_type: source.actor_type,
        actor_id: source.actor_id,
        rationale: source.rationale,
        source_reference: source.source_reference,
        changed_at: changedAt,
      },
      field_sources: fieldSources,
      latest_change: {
        id: source.change_id ?? 0,
        version: 2,
        actor_type: 'human',
        actor_id: 'preview-user',
        source_reference: source.source_reference ?? '',
        rationale: source.rationale ?? '',
        changed_fields: ['guidance'],
        applied_at: changedAt,
        reverted_from_id: null,
      },
    };
    return {
      policy,
      history: {
        items: [
          {
            id: source.change_id ?? 0,
            version: 2,
            actor_type: 'human',
            actor_id: 'preview-user',
            source_reference: source.source_reference ?? '',
            rationale: 'Replace guidance that used an old priority list.',
            changed_fields: ['guidance'],
            applied_at: changedAt,
            reverted_from_id: null,
            workspace_id: 'preview-org',
            policy_kind: 'cycle',
            target_type: 'cycle',
            target_id: String(cycle.id),
            before_snapshot: {
              configuration,
              guidance: [...activeGuidance, 'Use the legacy priority list before reviewing the workspace.'],
            },
            after_snapshot: { configuration, guidance: activeGuidance },
            cycle_revision_id: source.cycle_revision_id ?? 0,
          },
        ],
        pagination: { limit: 50, offset: 0, has_more: false, next_offset: null },
      },
    };
  }

  function setPreviewBehaviorPolicy(cycle: CycleRead) {
    const { policy, history } = previewBehaviorPolicy(cycle);
    previewPolicies = { ...previewPolicies, [cycle.id]: policy };
    previewPolicyHistories = { ...previewPolicyHistories, [cycle.id]: history };
  }

  function loadPreviewData() {
    const previewCycles = [
      previewCycle(901, {
        name: 'Morning priority sweep',
        prompt: 'Review active Cortex thoughts, summarize what needs attention, and continue in the same planning thread.',
        schedule_expr: '0 9 * * 1-5',
        schedule_human: 'Weekdays at 9:00 AM',
        next_run_at: previewIso(1),
        last_run_at: previewIso(-1),
        last_status: 'completed',
      }),
      previewCycle(902, {
        name: 'Weekly evidence audit',
        prompt: 'Check recent completed runs for missing evidence and mark anything that needs follow-up.',
        schedule_expr: '30 15 * * 5',
        schedule_human: 'Fridays at 3:30 PM',
        next_run_at: previewIso(3),
        last_run_at: previewIso(-4),
        last_status: 'completed',
        thinking_override: 'high',
      }),
      previewCycle(903, {
        name: 'Domain CRM cleanup',
        prompt: 'Revisit outreach domain records and find stale leads, incomplete hooks, or missing next actions.',
        schedule_expr: '0 11 * * *',
        schedule_human: 'Every day at 11:00 AM',
        next_run_at: null,
        last_run_at: previewIso(-2),
        last_status: 'failed',
        last_error: 'Preview failure: domain source was unavailable.',
      }),
      previewCycle(904, {
        name: 'Monthly memory review',
        prompt: 'Distill recent daily notes into durable memory updates.',
        schedule_expr: '0 10 1 * *',
        schedule_human: 'Monthly on day 1 at 10:00 AM',
        enabled: false,
        next_run_at: null,
        last_run_at: previewIso(-20),
        last_status: 'skipped',
      }),
    ];

    cycles = previewCycles;
    for (const cycle of previewCycles) setPreviewBehaviorPolicy(cycle);
    previewRuns = {
      901: [
        previewRun(7101, 901, 'completed', previewCycles[0].prompt, -1),
        previewRun(7100, 901, 'completed', previewCycles[0].prompt, -2),
      ],
      902: [previewRun(7201, 902, 'completed', previewCycles[1].prompt, -4)],
      903: [previewRun(7301, 903, 'failed', previewCycles[2].prompt, -2)],
      904: [previewRun(7401, 904, 'skipped', previewCycles[3].prompt, -20)],
    };
    selectedCycleId = previewCycles[0].id;
    selectedRowId = cycleRowId(previewCycles[0]);
    fillForm(previewCycles[0]);
    runs = previewRuns[previewCycles[0].id] ?? [];
    loading = false;
  }

  function formatDateTime(value: string | null | undefined): string {
    if (!value) return '--';
    const date = parseServerDate(value);
    if (!date) return '--';
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(date);
  }

  function timeAgo(value: string | null | undefined): string {
    return relativeTimeAgo(value) || '--';
  }

  function sentenceCase(value: string | null | undefined): string {
    if (!value) return 'Idle';
    return value.charAt(0).toUpperCase() + value.slice(1).replaceAll('_', ' ');
  }

  function friendlyTimezone(value: string): string {
    return value.replaceAll('_', ' ');
  }

  function pad2(value: number): string {
    return String(value).padStart(2, '0');
  }

  function dateInputFromDate(date: Date): string {
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  }

  function defaultRunDate(): string {
    return dateInputFromDate(new Date(Date.now() + 86400000));
  }

  function isOneTimeSchedule(expr: string | null | undefined): boolean {
    return String(expr ?? '').trim().toLowerCase().startsWith(ONE_TIME_PREFIX);
  }

  function oneTimeDateFromExpression(expr: string | null | undefined): Date | null {
    if (!isOneTimeSchedule(expr)) return null;
    const raw = String(expr ?? '').trim().slice(ONE_TIME_PREFIX.length).trim();
    if (!raw) return null;
    const date = new Date(raw);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function oneTimeExpressionFromForm(): string {
    const date = form.date || defaultRunDate();
    const time = normalizeTime(form.time);
    return `${ONE_TIME_PREFIX}${date}T${time}:00`;
  }

  function formatDateLabel(value: string): string {
    const [year, month, day] = value.split('-').map(Number);
    if (!year || !month || !day) return value || 'selected date';
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(
      new Date(year, month - 1, day),
    );
  }

  function cycleDescription(cycle: CycleRead): string {
    const prompt = cycle.prompt.trim();
    if (!prompt) return scheduleLabelForCycle(cycle);
    return prompt.length > 116 ? `${prompt.slice(0, 116).trim()}...` : prompt;
  }

  function isIntegerToken(value: string): boolean {
    return /^\d+$/.test(value);
  }

  function normalizeTime(value: string | null | undefined): string {
    const match = String(value ?? '').match(/^(\d{1,2}):(\d{2})$/);
    if (!match) return DEFAULT_TIME;
    const hour = Number(match[1]);
    const minute = Number(match[2]);
    if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return DEFAULT_TIME;
    return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  }

  function timeFromCron(minute: string, hour: string): string | null {
    if (!isIntegerToken(minute) || !isIntegerToken(hour)) return null;
    const nextMinute = Number(minute);
    const nextHour = Number(hour);
    if (nextMinute < 0 || nextMinute > 59 || nextHour < 0 || nextHour > 23) return null;
    return `${String(nextHour).padStart(2, '0')}:${String(nextMinute).padStart(2, '0')}`;
  }

  function cronPartsFromTime(value: string): { minute: string; hour: string } {
    const normalized = normalizeTime(value);
    const [hour, minute] = normalized.split(':');
    return { minute: String(Number(minute)), hour: String(Number(hour)) };
  }

  function weekdayOption(value: string) {
    return WEEKDAY_OPTIONS.find((option) => option.value === value) ?? WEEKDAY_OPTIONS[1];
  }

  function formatTimeLabel(value: string): string {
    const normalized = normalizeTime(value);
    const [hour, minute] = normalized.split(':').map(Number);
    const date = new Date(2026, 0, 1, hour, minute);
    return new Intl.DateTimeFormat(undefined, {
      hour: 'numeric',
      minute: '2-digit',
    }).format(date);
  }

  function parseSchedule(expr: string | null | undefined): ParsedSchedule {
    const source = (expr || DEFAULT_SCHEDULE).trim();
    const onceDate = oneTimeDateFromExpression(source);
    if (onceDate) {
      return {
        cadence: 'once',
        date: dateInputFromDate(onceDate),
        time: normalizeTime(`${onceDate.getHours()}:${pad2(onceDate.getMinutes())}`),
        weekday: '1',
        monthday: '1',
        custom_schedule: source,
      };
    }

    const [minute, hour, dayOfMonth, month, dayOfWeek, ...extra] = source.split(/\s+/);
    const time = timeFromCron(minute, hour);

    if (!time || !dayOfMonth || !month || !dayOfWeek || extra.length) {
      return {
        cadence: 'custom',
        date: defaultRunDate(),
        time: DEFAULT_TIME,
        weekday: '1',
        monthday: '1',
        custom_schedule: source || DEFAULT_SCHEDULE,
      };
    }

    if (dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
      return {
        cadence: 'daily',
        date: defaultRunDate(),
        time,
        weekday: '1',
        monthday: '1',
        custom_schedule: source,
      };
    }

    if (
      dayOfMonth === '*' &&
      month === '*' &&
      ['1-5', '1,2,3,4,5'].includes(dayOfWeek)
    ) {
      return {
        cadence: 'weekdays',
        date: defaultRunDate(),
        time,
        weekday: '1',
        monthday: '1',
        custom_schedule: source,
      };
    }

    if (dayOfMonth === '*' && month === '*' && isIntegerToken(dayOfWeek)) {
      const normalizedWeekday = dayOfWeek === '7' ? '0' : dayOfWeek;
      if (WEEKDAY_OPTIONS.some((option) => option.value === normalizedWeekday)) {
        return {
          cadence: 'weekly',
          date: defaultRunDate(),
          time,
          weekday: normalizedWeekday,
          monthday: '1',
          custom_schedule: source,
        };
      }
    }

    if (month === '*' && dayOfWeek === '*' && isIntegerToken(dayOfMonth)) {
      const day = Number(dayOfMonth);
      if (day >= 1 && day <= 31) {
        return {
          cadence: 'monthly',
          date: defaultRunDate(),
          time,
          weekday: '1',
          monthday: String(day),
          custom_schedule: source,
        };
      }
    }

    return {
      cadence: 'custom',
      date: defaultRunDate(),
      time,
      weekday: '1',
      monthday: '1',
      custom_schedule: source,
    };
  }

  function scheduleExprFromForm(): string {
    if (form.cadence === 'once') return oneTimeExpressionFromForm();
    if (form.cadence === 'custom') return form.custom_schedule.trim();

    const { minute, hour } = cronPartsFromTime(form.time);
    if (form.cadence === 'weekdays') return `${minute} ${hour} * * 1-5`;
    if (form.cadence === 'weekly') return `${minute} ${hour} * * ${form.weekday}`;
    if (form.cadence === 'monthly') return `${minute} ${hour} ${form.monthday} * *`;
    return `${minute} ${hour} * * *`;
  }

  function scheduleLabelForForm(value: CycleForm): string {
    const time = formatTimeLabel(value.time);
    if (value.cadence === 'once') return `Once on ${formatDateLabel(value.date)} at ${time}`;
    if (value.cadence === 'weekdays') return `Weekdays at ${time}`;
    if (value.cadence === 'weekly') return `${weekdayOption(value.weekday).plural} at ${time}`;
    if (value.cadence === 'monthly') return `Monthly on day ${value.monthday} at ${time}`;
    if (value.cadence === 'custom') return 'Custom schedule';
    return `Every day at ${time}`;
  }

  function scheduleLabelForCycle(cycle: CycleRead): string {
    const parsed = parseSchedule(cycle.schedule_expr);
    if (parsed.cadence === 'custom') return cycle.schedule_human || 'Custom schedule';

    return scheduleLabelForForm({
      ...emptyForm(),
      ...parsed,
      name: cycle.name,
      prompt: cycle.prompt,
      schedule_expr: cycle.schedule_expr,
      timezone: cycle.timezone || localTimezone,
      enabled: cycle.enabled,
      model_override: cycle.model_override || '',
      thinking_override: (cycle.thinking_override as ThinkingLevel) || '',
      target_idea_id: cycle.target_idea_id || '',
    });
  }

  function threadLabel(cycle: CycleRead): string {
    return cycle.target_idea_id ? 'Same thread' : 'Thread after first run';
  }

  function fillForm(cycle: CycleRead | null) {
    if (!cycle) {
      form = emptyForm();
      advancedOpen = false;
      return;
    }

    const parsed = parseSchedule(cycle.schedule_expr);
    form = {
      name: cycle.name,
      prompt: cycle.prompt,
      schedule_expr: cycle.schedule_expr,
      cadence: parsed.cadence,
      date: parsed.date,
      time: parsed.time,
      weekday: parsed.weekday,
      monthday: parsed.monthday,
      custom_schedule: parsed.custom_schedule,
      timezone: cycle.timezone || localTimezone,
      enabled: cycle.enabled,
      model_override: cycle.model_override || '',
      thinking_override: (cycle.thinking_override as ThinkingLevel) || '',
      target_idea_id: cycle.target_idea_id || '',
    };
    advancedOpen = parsed.cadence === 'custom' || Boolean(cycle.model_override || cycle.thinking_override);
  }

  function setCadence(cadence: ScheduleCadence) {
    const previousSchedule = scheduleExprFromForm() || form.schedule_expr || DEFAULT_SCHEDULE;
    form.cadence = cadence;
    if (cadence === 'custom') {
      form.custom_schedule = form.custom_schedule.trim() || previousSchedule;
      advancedOpen = true;
    }
  }

  async function loadRuns(cycleId: number | null) {
    if (!cycleId) {
      runs = [];
      return;
    }
    if (isCyclesPreview) {
      runs = previewRuns[cycleId] ?? [];
      return;
    }
    runsLoading = true;
    runs = [];
    try {
      runs = await api.listCycleRuns(cycleId, 20);
    } catch (err: any) {
      runs = [];
      ui.toast(err.detail || 'Failed to load cycle runs', 'error');
    } finally {
      runsLoading = false;
    }
  }

  async function loadCycles(preferredCycleId: number | null = selectedCycleId) {
    if (isCyclesPreview) {
      loadPreviewData();
      return;
    }
    loading = true;
    loadError = '';
    try {
      const nextCycles = await api.listCycles();
      cycles = nextCycles;

      const nextSelectedCycle = preferredCycleId
        ? (nextCycles.find((cycle) => cycle.id === preferredCycleId) ?? null)
        : null;

      if (nextSelectedCycle) {
        selectedCycleId = nextSelectedCycle.id;
        selectedRowId = cycleRowId(nextSelectedCycle);
        fillForm(nextSelectedCycle);
        await loadRuns(nextSelectedCycle.id);
      } else if (selectedRowId !== 'draft') {
        selectedCycleId = null;
        selectedRowId = null;
        runs = [];
        fillForm(null);
      }
    } catch (err: any) {
      loadError = err.detail || 'Failed to load cycles';
      ui.toast(loadError, 'error');
      cycles = [];
      runs = [];
      selectedCycleId = null;
      selectedRowId = null;
      fillForm(null);
    } finally {
      loading = false;
    }
  }

  function createNewCycle() {
    selectedCycleId = null;
    selectedRowId = null;
    runs = [];
    fillForm(null);
    showCreateModal = true;
  }

  function closeCreateModal() {
    showCreateModal = false;
    if (!selectedCycleId) {
      fillForm(null);
    }
  }

  async function selectCycle(cycleId: number) {
    showCreateModal = false;
    const cycle = cycles.find((item) => item.id === cycleId) ?? null;
    if (!cycle) return;
    const rowId = cycleRowId(cycle);
    if (selectedRowId === rowId) {
      selectedCycleId = null;
      selectedRowId = null;
      runs = [];
      fillForm(null);
      return;
    }
    selectedCycleId = cycleId;
    selectedRowId = rowId;
    fillForm(cycle);
    await loadRuns(cycleId);
  }

  async function saveCycle() {
    const isCreate = !selectedCycleId;
    const scheduleExpr = scheduleExprFromForm();
    const payload = {
      name: form.name.trim(),
      prompt: form.prompt.trim(),
      schedule_expr: scheduleExpr,
      timezone: localTimezone,
      enabled: form.enabled,
      model_override: form.model_override.trim() || null,
      thinking_override: form.thinking_override || null,
      execution_mode: 'reuse_same_idea' as const,
      target_idea_id: form.target_idea_id || selectedRunThreadId || null,
      reopen_archived: true,
    };

    if (!payload.name || !payload.prompt || !payload.schedule_expr) {
      ui.toast('Name, prompt, and schedule are required', 'error');
      return;
    }

    saving = true;
    try {
      if (isCyclesPreview) {
        const now = new Date().toISOString();
        const savedId = selectedCycleId ?? Math.max(900, ...cycles.map((cycle) => cycle.id)) + 1;
        const existing = cycles.find((cycle) => cycle.id === savedId);
        const saved = previewCycle(savedId, {
          ...existing,
          name: payload.name,
          prompt: payload.prompt,
          schedule_expr: payload.schedule_expr,
          schedule_human: scheduleLabelForForm(form),
          timezone: localTimezone,
          enabled: payload.enabled,
          model_override: payload.model_override,
          thinking_override: payload.thinking_override,
          target_idea_id: payload.target_idea_id ?? existing?.target_idea_id ?? `preview-cycle-${savedId}`,
          next_run_at: previewNextRunAtForSchedule(payload.schedule_expr, payload.enabled),
          last_run_at: existing?.last_run_at ?? null,
          last_status: existing?.last_status ?? 'idle',
          last_error: null,
          created_at: existing?.created_at ?? now,
          updated_at: now,
        });
        cycles = existing
          ? cycles.map((cycle) => (cycle.id === savedId ? saved : cycle))
          : [saved, ...cycles];
        setPreviewBehaviorPolicy(saved);
        selectedCycleId = saved.id;
        selectedRowId = cycleRowId(saved);
        fillForm(saved);
        runs = previewRuns[saved.id] ?? [];
        if (isCreate) showCreateModal = false;
        ui.toast(existing ? 'Preview cycle updated' : 'Preview cycle created', 'success');
        return;
      }
      const saved = selectedCycleId
        ? await api.updateCycle(selectedCycleId, payload)
        : await api.createCycle(payload);
      ui.toast(selectedCycleId ? 'Cycle updated' : 'Cycle created', 'success');
      if (isCreate) showCreateModal = false;
      await loadCycles(saved.id);
      behaviorPolicyRefreshSerial += 1;
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to save cycle', 'error');
    } finally {
      saving = false;
    }
  }

  async function deleteSelectedCycle() {
    if (!selectedCycleId || !selectedCycle) return;
    const confirmed = window.confirm(`Delete cycle "${selectedCycle.name}"?`);
    if (!confirmed) return;
    deleting = true;
    try {
      if (isCyclesPreview) {
        cycles = cycles.filter((cycle) => cycle.id !== selectedCycleId);
        const { [selectedCycleId]: _removed, ...nextPreviewRuns } = previewRuns;
        previewRuns = nextPreviewRuns;
        const { [selectedCycleId]: _removedPolicy, ...nextPreviewPolicies } = previewPolicies;
        const { [selectedCycleId]: _removedHistory, ...nextPreviewHistories } = previewPolicyHistories;
        previewPolicies = nextPreviewPolicies;
        previewPolicyHistories = nextPreviewHistories;
        selectedCycleId = null;
        selectedRowId = null;
        runs = [];
        fillForm(null);
        ui.toast('Preview cycle deleted', 'success');
        return;
      }
      await api.deleteCycle(selectedCycleId);
      ui.toast('Cycle deleted', 'success');
      await loadCycles(null);
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to delete cycle', 'error');
    } finally {
      deleting = false;
    }
  }

  async function runNow(cycleId: number) {
    try {
      if (isCyclesPreview) {
        const cycle = cycles.find((item) => item.id === cycleId);
        if (!cycle) return;
        const runId = Date.now() % 100000;
        const run = previewRun(runId, cycleId, 'completed', cycle.prompt, 0);
        previewRuns = {
          ...previewRuns,
          [cycleId]: [run, ...(previewRuns[cycleId] ?? [])],
        };
        cycles = cycles.map((item) =>
          item.id === cycleId
            ? { ...item, last_run_at: run.created_at, last_status: 'completed', last_error: null }
            : item,
        );
        runs = previewRuns[cycleId] ?? [];
        ui.toast('Preview cycle launched', 'success');
        return;
      }
      await api.runCycle(cycleId);
      ui.toast('Cycle launched', 'success');
      await loadCycles(cycleId);
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to run cycle', 'error');
    }
  }

  async function toggleEnabled(cycle: CycleRead) {
    const preferredCycleId = selectedCycleId;
    try {
      if (isCyclesPreview) {
        cycles = cycles.map((item) =>
          item.id === cycle.id
            ? {
                ...item,
                enabled: !item.enabled,
                next_run_at: !item.enabled ? previewNextRunAtForSchedule(item.schedule_expr, true) : null,
                updated_at: new Date().toISOString(),
              }
            : item,
        );
        const updated = cycles.find((item) => item.id === cycle.id);
        if (updated && preferredCycleId === updated.id) {
          fillForm(updated);
          setPreviewBehaviorPolicy(updated);
        }
        ui.toast(updated?.enabled ? 'Preview cycle resumed' : 'Preview cycle paused', 'success');
        return;
      }
      await api.updateCycle(cycle.id, { enabled: !cycle.enabled });
      await loadCycles(preferredCycleId);
      behaviorPolicyRefreshSerial += 1;
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to update cycle', 'error');
    }
  }

  onMount(async () => {
    if (isCyclesPreview) {
      loadPreviewData();
      return;
    }
    await loadCycles();
  });
</script>

<ConstellationPageFrame
  eyebrow="Constellation Cycles"
  title="Cycles"
  subtitle="Schedule recurring prompts."
  contentClassName="cycles-page"
>
  {#snippet actions()}
    <ConstellationButton variant="secondary" size="sm" onclick={createNewCycle}>
      New cycle
    </ConstellationButton>
  {/snippet}

  {#if loadError}
    <ConstellationNotice title="Cycles failed to load." description={loadError} tone="danger">
      {#snippet actions()}
        <ConstellationButton variant="secondary" size="sm" onclick={() => loadCycles(selectedCycleId)}>Retry</ConstellationButton>
      {/snippet}
    </ConstellationNotice>
  {/if}

  <section class="workspace">
    <section class="inventory-panel" aria-label="Cycle inventory">
      <div class="inventory-tools">
        <ConstellationSearchField bind:value={search} placeholder="Search cycles..." aria-label="Search cycles" />
        <ConstellationSegmentedToggle
          options={FILTER_OPTIONS}
          activeKey={filterMode}
          onActiveKeyChange={setFilter}
          ariaLabel="Cycle filter"
        />
      </div>

      <div class="cycle-list" aria-label="Cycles">
        {#if loading}
          {#each Array(7) as _}
            <div class="cycle-row-skeleton"></div>
          {/each}
        {:else if !cycles.length}
          <ConstellationEmptyState
            title="No cycles yet"
            size="sm"
            surface="plain"
          />
        {:else if filteredCycles.length === 0}
          <ConstellationEmptyState
            title="No matching cycles"
            size="sm"
            surface="plain"
          />
        {:else}
          {#each filteredCycles as cycle (cycle.id)}
            <article class="cycle-item" class:is-expanded={selectedRowId === cycleRowId(cycle)}>
              <button
                type="button"
                class="cycle-row"
                class:is-selected={selectedRowId === cycleRowId(cycle)}
                onclick={() => selectCycle(cycle.id)}
                aria-pressed={selectedRowId === cycleRowId(cycle)}
              >
                <span class="cycle-row-main">
                  <strong>{cycle.name}</strong>
                  <small>{cycleDescription(cycle)}</small>
                </span>
                <span class="cycle-row-side">
                  <ConstellationPill variant={cycleStatusVariant(cycle)} leadingDot>
                    {cycleStatusLabel(cycle)}
                  </ConstellationPill>
                  <small>{cycleStatusDetail(cycle)}</small>
                </span>
              </button>

              {#if selectedRowId === cycleRowId(cycle)}
                <div class="cycle-expanded">
                  <div class="expanded-toolbar">
                    <div class="expanded-facts" aria-label="Cycle facts">
                      <span>{scheduleLabelForCycle(cycle)}</span>
                      <span>{cycleFactSummary(cycle)}</span>
                      <span>{runs.length} runs</span>
                      <span>{friendlyTimezone(cycle.timezone || localTimezone)}</span>
                    </div>
                    <div class="expanded-actions">
                      <ConstellationButton variant="quiet" size="sm" onclick={() => runNow(cycle.id)}>
                        Run now
                      </ConstellationButton>
                      <ConstellationButton variant="quiet" size="sm" onclick={() => toggleEnabled(cycle)}>
                        {cycle.enabled ? 'Pause' : 'Resume'}
                      </ConstellationButton>
                      <ConstellationButton variant="secondary" size="sm" loading={saving} onclick={saveCycle}>
                        Save
                      </ConstellationButton>
                      <ConstellationButton
                        variant="destructive"
                        size="sm"
                        loading={deleting}
                        onclick={deleteSelectedCycle}
                      >
                        Delete
                      </ConstellationButton>
                    </div>
                  </div>

                  {#if selectedCycle?.last_error}
                    <ConstellationNotice
                      title="Latest run ended with an error"
                      description={selectedCycle.last_error}
                      tone="danger"
                    />
                  {/if}

                  <EffectiveCyclePolicyView
                    cycleId={cycle.id}
                    previewPolicy={isCyclesPreview ? previewPolicies[cycle.id] : null}
                    previewHistory={isCyclesPreview ? previewPolicyHistories[cycle.id] : null}
                    displayTimezone={isCyclesPreview ? localTimezone : null}
                    refreshSerial={behaviorPolicyRefreshSerial}
                  />

                  <details class="cycle-region" open>
                    <summary>
                      <span>Editor</span>
                      <small>{schedulePreview}</small>
                    </summary>
                    <div class="cycle-form">
                      <section class="cycle-form-section" aria-labelledby={`cycle-${cycle.id}-basics-heading`}>
                        <div class="cycle-form-section-heading">
                          <h3 id={`cycle-${cycle.id}-basics-heading`}>Basics</h3>
                        </div>

                        <div class="cycle-form-grid">
                          <label class="cycle-field cycle-field-full">
                            <span class="cycle-field-label">Name</span>
                            <input bind:value={form.name} class="cycle-input" placeholder="Morning briefing" />
                          </label>

                          <label class="cycle-field cycle-field-full">
                            <span class="cycle-field-label">Prompt</span>
                            <AiPromptComposer
                              bind:value={form.prompt}
                              className="cycle-prompt-composer"
                              rows={7}
                              minHeight={176}
                              maxHeight={320}
                              slashPlacement="below"
                              ariaLabel="Cycle prompt"
                              placeholder="Review my latest priorities and tell me what needs attention."
                            />
                          </label>
                        </div>
                      </section>

                      <section class="cycle-form-section" aria-labelledby={`cycle-${cycle.id}-schedule-heading`}>
                        <div class="cycle-form-section-heading">
                          <h3 id={`cycle-${cycle.id}-schedule-heading`}>When</h3>
                          <p>{schedulePreview}</p>
                        </div>

                        <div class="cadence-grid" role="group" aria-label="Schedule frequency">
                          {#each CADENCE_OPTIONS as option}
                            <button
                              type="button"
                              class="cadence-option"
                              class:active={form.cadence === option.value}
                              aria-pressed={form.cadence === option.value}
                              onclick={() => setCadence(option.value)}
                            >
                              <span>{option.label}</span>
                              <small>{option.description}</small>
                            </button>
                          {/each}
                        </div>

                        {#if form.cadence !== 'custom'}
                          <div class="schedule-fields">
                            {#if form.cadence === 'once'}
                              <label class="cycle-field">
                                <span class="cycle-field-label">Date</span>
                                <input bind:value={form.date} class="cycle-input" type="date" />
                              </label>
                            {/if}

                            <label class="cycle-field">
                              <span class="cycle-field-label">Time</span>
                              <input bind:value={form.time} class="cycle-input" type="time" />
                            </label>

                            {#if form.cadence === 'weekly'}
                              <label class="cycle-field">
                                <span class="cycle-field-label">Day</span>
                                <select bind:value={form.weekday} class="cycle-select">
                                  {#each WEEKDAY_OPTIONS as option}
                                    <option value={option.value}>{option.label}</option>
                                  {/each}
                                </select>
                              </label>
                            {:else if form.cadence === 'monthly'}
                              <label class="cycle-field">
                                <span class="cycle-field-label">Day of month</span>
                                <select bind:value={form.monthday} class="cycle-select">
                                  {#each MONTHDAY_OPTIONS as day}
                                    <option value={day}>{day}</option>
                                  {/each}
                                </select>
                              </label>
                            {/if}
                          </div>
                        {/if}

                        <p class="local-time-note">
                          Runs in your local time: {friendlyTimezone(localTimezone)}
                        </p>
                      </section>

                      <section class="cycle-form-section" aria-labelledby={`cycle-${cycle.id}-thread-heading`}>
                        <div class="cycle-form-section-heading">
                          <h3 id={`cycle-${cycle.id}-thread-heading`}>Thread</h3>
                        </div>

                        <div class="thread-status">
                          <div>
                            <strong>
                              {#if selectedThreadId}
                                Continues in the same thread
                              {:else}
                                Thread will be created on first run
                              {/if}
                            </strong>
                            <p>
                              {#if selectedThreadId}
                                Future runs return here, even if the thread gets archived.
                              {:else}
                                After the first run, this cycle will keep using that thread automatically.
                              {/if}
                            </p>
                          </div>

                          {#if selectedThreadId}
                            <a class="cycle-link" href={`/cortex?idea=${selectedThreadId}`}>
                              Open thread
                            </a>
                          {/if}
                        </div>
                      </section>

                      <section class="cycle-form-section" aria-labelledby={`cycle-${cycle.id}-status-heading`}>
                        <div class="cycle-form-section-heading">
                          <h3 id={`cycle-${cycle.id}-status-heading`}>Status</h3>
                        </div>

                        <button
                          type="button"
                          class="status-switch"
                          class:active={form.enabled}
                          aria-pressed={form.enabled}
                          onclick={() => (form.enabled = !form.enabled)}
                        >
                          <span aria-hidden="true"></span>
                          <strong>{form.enabled ? 'Active' : 'Paused'}</strong>
                          <small>
                            {form.enabled
                              ? 'Runs automatically on the schedule above.'
                              : 'Kept for later, but will not run automatically.'}
                          </small>
                        </button>
                      </section>

                      <section class="cycle-advanced">
                        <button
                          type="button"
                          class="advanced-toggle"
                          aria-expanded={advancedOpen}
                          onclick={() => (advancedOpen = !advancedOpen)}
                        >
                          <span>Advanced settings</span>
                          <span aria-hidden="true">{advancedOpen ? 'Hide' : 'Show'}</span>
                        </button>

                        {#if advancedOpen}
                          <div class="advanced-fields">
                            {#if form.cadence === 'custom'}
                              <label class="cycle-field cycle-field-full">
                                <span class="cycle-field-label">Custom schedule</span>
                                <input
                                  bind:value={form.custom_schedule}
                                  class="cycle-input cycle-mono"
                                  placeholder="0 9 * * *"
                                />
                                <span class="cycle-field-hint">
                                  Use this only when Once, Daily, Weekdays, Weekly, or Monthly does not cover the cycle.
                                </span>
                              </label>
                            {/if}

                            <label class="cycle-field">
                              <span class="cycle-field-label">Model override</span>
                              <input
                                bind:value={form.model_override}
                                class="cycle-input cycle-mono"
                                placeholder="Use default"
                              />
                            </label>

                            <label class="cycle-field">
                              <span class="cycle-field-label">Reasoning</span>
                              <select bind:value={form.thinking_override} class="cycle-select">
                                {#each THINKING_OPTIONS as option}
                                  <option value={option.value}>{option.label}</option>
                                {/each}
                              </select>
                            </label>
                          </div>
                        {/if}
                      </section>

                      <div class="cycle-form-actions">
                        <ConstellationButton variant="primary" loading={saving} onclick={saveCycle}>
                          Save cycle
                        </ConstellationButton>
                      </div>
                    </div>
                  </details>

                  <details class="cycle-region" open>
                    <summary>
                      <span>Recent runs</span>
                      <small>{runsLoading ? 'Loading' : `${runs.length} recent`}</small>
                    </summary>
                    {#if runsLoading}
                      <div class="runs-list">
                        <div class="run-row-skeleton"></div>
                        <div class="run-row-skeleton"></div>
                      </div>
                    {:else if !runs.length}
                      <p class="empty-inline">No runs yet.</p>
                    {:else}
                      <div class="runs-list">
                        {#each runs as run}
                          <article class="run-row">
                            <div class="run-row-main">
                              <span class="run-row-eyebrow">Run #{run.id}</span>
                              <strong>{formatDateTime(run.scheduled_for)}</strong>
                              <p>{run.prompt_snapshot}</p>
                            </div>

                            <div class="run-row-meta">
                              <ConstellationPill variant={toneForStatus(run.status)}>
                                {sentenceCase(run.status)}
                              </ConstellationPill>
                              <span>{timeAgo(run.created_at)}</span>
                              {#if run.idea_id}
                                <a class="cycle-link" href={`/cortex?idea=${run.idea_id}`}>Open thread</a>
                              {/if}
                              {#if run.skip_reason}
                                <span>{run.skip_reason}</span>
                              {/if}
                            </div>
                          </article>
                        {/each}
                      </div>
                    {/if}
                  </details>
                </div>
              {/if}
            </article>
          {/each}
        {/if}
      </div>
    </section>
  </section>
</ConstellationPageFrame>

{#if showCreateModal}
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-overlay" onclick={closeCreateModal} role="dialog" aria-modal="true" tabindex="-1">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal cycle-modal" onclick={(event) => event.stopPropagation()}>
      <div class="modal-header">
        <span class="modal-title">New cycle</span>
        <button class="modal-close" onclick={closeCreateModal} aria-label="Close new cycle form">&times;</button>
      </div>

      <form class="cycle-form" onsubmit={(event) => { event.preventDefault(); saveCycle(); }}>
        <section class="cycle-form-section" aria-labelledby="cycle-create-basics-heading">
          <div class="cycle-form-section-heading">
            <h3 id="cycle-create-basics-heading">Basics</h3>
          </div>

          <div class="cycle-form-grid">
            <label class="cycle-field cycle-field-full">
              <span class="cycle-field-label">Name</span>
              <input bind:value={form.name} class="cycle-input" placeholder="Morning briefing" />
            </label>

            <label class="cycle-field cycle-field-full">
              <span class="cycle-field-label">Prompt</span>
              <AiPromptComposer
                bind:value={form.prompt}
                className="cycle-prompt-composer"
                rows={7}
                minHeight={176}
                maxHeight={320}
                slashPlacement="below"
                ariaLabel="Cycle prompt"
                placeholder="Review my latest priorities and tell me what needs attention."
              />
            </label>
          </div>
        </section>

        <section class="cycle-form-section" aria-labelledby="cycle-create-schedule-heading">
          <div class="cycle-form-section-heading">
            <h3 id="cycle-create-schedule-heading">When</h3>
            <p>{schedulePreview}</p>
          </div>

          <div class="cadence-grid" role="group" aria-label="Schedule frequency">
            {#each CADENCE_OPTIONS as option}
              <button
                type="button"
                class="cadence-option"
                class:active={form.cadence === option.value}
                aria-pressed={form.cadence === option.value}
                onclick={() => setCadence(option.value)}
              >
                <span>{option.label}</span>
                <small>{option.description}</small>
              </button>
            {/each}
          </div>

          {#if form.cadence !== 'custom'}
            <div class="schedule-fields">
              {#if form.cadence === 'once'}
                <label class="cycle-field">
                  <span class="cycle-field-label">Date</span>
                  <input bind:value={form.date} class="cycle-input" type="date" />
                </label>
              {/if}

              <label class="cycle-field">
                <span class="cycle-field-label">Time</span>
                <input bind:value={form.time} class="cycle-input" type="time" />
              </label>

              {#if form.cadence === 'weekly'}
                <label class="cycle-field">
                  <span class="cycle-field-label">Day</span>
                  <select bind:value={form.weekday} class="cycle-select">
                    {#each WEEKDAY_OPTIONS as option}
                      <option value={option.value}>{option.label}</option>
                    {/each}
                  </select>
                </label>
              {:else if form.cadence === 'monthly'}
                <label class="cycle-field">
                  <span class="cycle-field-label">Day of month</span>
                  <select bind:value={form.monthday} class="cycle-select">
                    {#each MONTHDAY_OPTIONS as day}
                      <option value={day}>{day}</option>
                    {/each}
                  </select>
                </label>
              {/if}
            </div>
          {/if}

          <p class="local-time-note">
            Runs in your local time: {friendlyTimezone(localTimezone)}
          </p>
        </section>

        <section class="cycle-form-section" aria-labelledby="cycle-create-status-heading">
          <div class="cycle-form-section-heading">
            <h3 id="cycle-create-status-heading">Status</h3>
          </div>

          <button
            type="button"
            class="status-switch"
            class:active={form.enabled}
            aria-pressed={form.enabled}
            onclick={() => (form.enabled = !form.enabled)}
          >
            <span aria-hidden="true"></span>
            <strong>{form.enabled ? 'Active' : 'Paused'}</strong>
            <small>
              {form.enabled
                ? 'Runs automatically on the schedule above.'
                : 'Kept for later, but will not run automatically.'}
            </small>
          </button>
        </section>

        <section class="cycle-advanced">
          <button
            type="button"
            class="advanced-toggle"
            aria-expanded={advancedOpen}
            onclick={() => (advancedOpen = !advancedOpen)}
          >
            <span>Advanced settings</span>
            <span aria-hidden="true">{advancedOpen ? 'Hide' : 'Show'}</span>
          </button>

          {#if advancedOpen}
            <div class="advanced-fields">
              {#if form.cadence === 'custom'}
                <label class="cycle-field cycle-field-full">
                  <span class="cycle-field-label">Custom schedule</span>
                  <input
                    bind:value={form.custom_schedule}
                    class="cycle-input cycle-mono"
                    placeholder="0 9 * * *"
                  />
                  <span class="cycle-field-hint">
                    Use this only when Once, Daily, Weekdays, Weekly, or Monthly does not cover the cycle.
                  </span>
                </label>
              {/if}

              <label class="cycle-field">
                <span class="cycle-field-label">Model override</span>
                <input
                  bind:value={form.model_override}
                  class="cycle-input cycle-mono"
                  placeholder="Use default"
                />
              </label>

              <label class="cycle-field">
                <span class="cycle-field-label">Reasoning</span>
                <select bind:value={form.thinking_override} class="cycle-select">
                  {#each THINKING_OPTIONS as option}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
              </label>
            </div>
          {/if}
        </section>

        <div class="cycle-form-actions">
          <ConstellationButton variant="quiet" onclick={closeCreateModal} disabled={saving}>
            Cancel
          </ConstellationButton>
          <ConstellationButton type="submit" variant="primary" loading={saving} loadingLabel="Creating">
            Create cycle
          </ConstellationButton>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  :global(.cycles-page) {
    gap: 14px;
  }

  .workspace {
    display: grid;
    grid-template-columns: 1fr;
    align-items: start;
    gap: 14px;
    min-height: 0;
  }

  .inventory-panel {
    display: grid;
    gap: 14px;
    min-height: 0;
    overflow: visible;
  }

  .inventory-tools {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    padding: 0 0 14px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .inventory-tools :global(.constellation-search-field) {
    flex: 1 1 260px;
  }

  .cycle-list {
    display: grid;
    align-content: start;
    gap: 8px;
    min-height: 0;
    overflow: visible;
    padding: 0;
  }

  .cycle-item {
    display: grid;
    min-width: 0;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: var(--constellation-surface-panel-background);
    overflow: hidden;
  }

  .cycle-item.is-expanded {
    border-color: var(--constellation-control-focus-ring);
  }

  .cycle-row,
  .cycle-row-skeleton {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
    min-height: 58px;
    width: 100%;
    min-width: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    color: var(--constellation-color-text-primary);
    padding: 10px 12px;
    text-align: left;
  }

  .cycle-row {
    cursor: pointer;
    transition:
      border-color var(--constellation-motion-settle-duration) ease,
      background-color var(--constellation-motion-settle-duration) ease,
      transform var(--constellation-motion-hover-duration) ease;
  }

  .cycle-row:hover,
  .cycle-row.is-selected {
    background: color-mix(in srgb, var(--constellation-color-text-primary) 3%, transparent);
  }

  .cycle-row-main,
  .cycle-row-side {
    display: grid;
    min-width: 0;
    gap: 7px;
  }

  .cycle-row-main strong,
  .cycle-row-main small,
  .cycle-row-side small {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cycle-row-main strong {
    font-size: 13px;
    font-weight: 560;
    letter-spacing: 0;
  }

  .cycle-row-main small,
  .cycle-row-side small,
  .empty-inline,
  .cycle-form-section-heading p,
  .cycle-field-hint,
  .local-time-note,
  .run-row-main p,
  .run-row-meta {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.5;
  }

  .cycle-row-side {
    justify-items: end;
  }

  .cycle-row-skeleton,
  .run-row-skeleton {
    min-height: 58px;
    border-radius: 8px;
    background:
      linear-gradient(90deg, transparent, var(--constellation-skeleton-row-shimmer), transparent),
      var(--constellation-skeleton-row-background);
    background-size: 200% 100%;
    animation: cycle-pulse 1.4s ease-in-out infinite;
  }

  .cycle-expanded {
    display: grid;
    grid-template-columns: 1fr;
    gap: 10px;
    padding: 0 12px 12px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .expanded-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding-top: 10px;
  }

  .expanded-facts,
  .expanded-actions,
  .cycle-form-actions,
  .run-row-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .expanded-facts span {
    padding: 2px 7px;
    border: 1px solid var(--constellation-control-button-secondary-border);
    border-radius: 999px;
    background: var(--constellation-control-button-secondary-background);
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    letter-spacing: 0;
  }

  .cycle-region {
    display: grid;
    min-width: 0;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: var(--constellation-surface-panel-background);
  }

  .cycle-region summary {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
    min-height: 42px;
    padding: 0 12px;
    color: var(--constellation-color-text-primary);
    cursor: pointer;
    list-style: none;
  }

  .cycle-region summary::-webkit-details-marker {
    display: none;
  }

  .cycle-region summary span,
  .cycle-form-section-heading h3,
  .thread-status strong,
  .status-switch strong,
  .advanced-toggle span:first-child,
  .run-row-main strong {
    color: var(--constellation-color-text-primary);
    font-size: 13px;
    font-weight: 560;
    letter-spacing: 0;
    line-height: 1.35;
  }

  .cycle-region summary small {
    min-width: 0;
    overflow: hidden;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    text-align: right;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cycle-region[open] summary {
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .cycle-region > :not(summary) {
    margin: 12px;
  }

  .cycle-form,
  .cycle-form-grid,
  .cadence-grid,
  .schedule-fields,
  .advanced-fields,
  .runs-list {
    display: grid;
    gap: 14px;
  }

  .cycle-form {
    gap: 18px;
  }

  .cycle-form-section {
    display: grid;
    gap: 12px;
    min-width: 0;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--constellation-section-divider);
  }

  .cycle-form-section-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
  }

  .cycle-form-section-heading h3,
  .cycle-form-section-heading p {
    margin: 0;
  }

  .cycle-form-section-heading p {
    text-align: right;
  }

  .cycle-field {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .cycle-field-full {
    grid-column: 1 / -1;
  }

  .cycle-field-label,
  .run-row-eyebrow {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.14em;
    line-height: 1.2;
    text-transform: uppercase;
  }

  .cycle-input,
  .cycle-select,
  :global(.cycle-prompt-composer) {
    width: 100%;
    box-sizing: border-box;
    border-radius: 8px;
    border: 1px solid var(--constellation-control-field-border);
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-primary);
    font-size: 14px;
    line-height: 1.45;
    box-shadow: var(--constellation-surface-nested-shadow);
  }

  .cycle-input,
  .cycle-select {
    min-height: 42px;
    padding: 10px 12px;
  }

  :global(.cycle-prompt-composer) {
    --ai-prompt-padding: 10px 12px;
    --ai-prompt-font-size: 14px;
    --ai-prompt-line-height: 1.45;
    --ai-prompt-text: var(--constellation-color-text-primary);
    --ai-prompt-placeholder: var(--constellation-control-field-placeholder);
  }

  .cycle-input:focus,
  .cycle-select:focus,
  :global(.cycle-prompt-composer:focus-within) {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
    border-color: var(--constellation-control-field-focus-border);
  }

  .cycle-input::placeholder {
    color: var(--constellation-control-field-placeholder);
  }

  .cycle-mono {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 13px;
  }

  .cycle-form-grid,
  .advanced-fields {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .cadence-grid {
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 8px;
  }

  .cadence-option {
    display: grid;
    gap: 5px;
    min-width: 0;
    min-height: 66px;
    padding: 10px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
    color: var(--constellation-color-text-primary);
    text-align: left;
    cursor: pointer;
    transition:
      border-color var(--constellation-motion-settle-duration) ease,
      background var(--constellation-motion-settle-duration) ease;
  }

  .cadence-option:hover,
  .cadence-option.active {
    border-color: var(--constellation-control-focus-ring);
    background: var(--constellation-control-button-secondary-background-hover);
  }

  .cadence-option:focus-visible,
  .cycle-row:focus-visible,
  .status-switch:focus-visible,
  .advanced-toggle:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .cadence-option span {
    font-size: 13px;
    font-weight: 560;
    line-height: 1.2;
  }

  .cadence-option small {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.3;
  }

  .schedule-fields {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    max-width: 520px;
  }

  .thread-status {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    min-width: 0;
    padding: 12px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-color-text-primary) 2%, transparent);
  }

  .thread-status p {
    margin: 5px 0 0;
  }

  .status-switch {
    display: grid;
    grid-template-columns: auto minmax(0, max-content) minmax(0, 1fr);
    align-items: center;
    gap: 12px;
    width: 100%;
    min-height: 54px;
    padding: 10px 12px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 8px;
    background: var(--constellation-surface-panel-background);
    color: var(--constellation-color-text-primary);
    text-align: left;
    cursor: pointer;
  }

  .status-switch > span {
    position: relative;
    width: 42px;
    height: 23px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--constellation-color-text-muted) 22%, transparent);
    transition: background var(--constellation-motion-settle-duration) ease;
  }

  .status-switch > span::after {
    content: '';
    position: absolute;
    top: 3px;
    left: 3px;
    width: 17px;
    height: 17px;
    border-radius: 999px;
    background: var(--constellation-color-text-primary);
    transition: transform var(--constellation-motion-settle-duration) ease;
  }

  .status-switch.active > span {
    background: color-mix(in srgb, var(--constellation-color-success) 42%, transparent);
  }

  .status-switch.active > span::after {
    transform: translateX(19px);
  }

  .cycle-advanced {
    display: grid;
    gap: 12px;
  }

  .advanced-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    width: 100%;
    min-height: 42px;
    padding: 0;
    border: 0;
    border-bottom: 1px solid var(--constellation-section-divider);
    background: transparent;
    color: var(--constellation-color-text-primary);
    cursor: pointer;
  }

  .advanced-toggle span:last-child,
  .cycle-link {
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .cycle-form-actions {
    justify-content: flex-end;
    padding-top: 2px;
  }

  .cycle-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 30px;
    text-decoration: none;
    white-space: nowrap;
  }

  .cycle-link:hover {
    text-decoration: underline;
  }

  .run-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 16px;
    min-width: 0;
    padding: 12px 0;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .run-row:last-child {
    border-bottom: 0;
  }

  .run-row-main {
    display: grid;
    gap: 6px;
    min-width: 0;
  }

  .run-row-main p {
    margin: 0;
  }

  .run-row-meta {
    align-content: start;
    justify-content: flex-end;
    text-align: right;
  }

  .cycle-modal {
    max-width: min(920px, calc(100vw - 36px));
    max-height: min(860px, calc(100vh - 36px));
    overflow: auto;
  }

  .cycle-modal :global(.cycle-prompt-composer) {
    min-height: 176px;
  }

  @keyframes cycle-pulse {
    0% {
      background-position: 200% 0;
    }
    100% {
      background-position: -200% 0;
    }
  }

  @media (max-width: 860px) {
    .cycle-row {
      grid-template-columns: 1fr;
    }

    .cycle-row-side {
      justify-items: start;
    }

    .cycle-form-grid,
    .advanced-fields {
      grid-template-columns: 1fr;
    }

    .cadence-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .schedule-fields {
      grid-template-columns: 1fr;
      max-width: none;
    }

    .cycle-form-section-heading,
    .thread-status {
      align-items: flex-start;
      flex-direction: column;
    }

    .cycle-form-section-heading p,
    .run-row-meta {
      text-align: left;
    }

    .run-row {
      grid-template-columns: 1fr;
    }

    .run-row-meta {
      justify-content: flex-start;
    }
  }

  @media (max-width: 560px) {
    .cadence-grid {
      grid-template-columns: 1fr;
    }

    .status-switch {
      grid-template-columns: auto 1fr;
    }

    .status-switch small {
      grid-column: 2;
    }
  }
</style>
