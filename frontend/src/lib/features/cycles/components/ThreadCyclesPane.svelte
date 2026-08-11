<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  import { api, type CycleRead, type CycleRunRead } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationIcon,
    ConstellationPill,
  } from '$lib/components/constellation';
  import { ui } from '$lib/stores/ui.svelte';
  import { wsClient } from '$lib/stores/ws.svelte';
  import EffectiveCyclePolicyView from './EffectiveCyclePolicyView.svelte';

  type Cadence = 'once' | 'daily' | 'weekdays' | 'weekly' | 'monthly' | 'custom';
  type FormState = {
    name: string;
    prompt: string;
    cadence: Cadence;
    date: string;
    time: string;
    weekday: string;
    monthday: string;
    customSchedule: string;
    enabled: boolean;
    modelOverride: string;
    thinkingOverride: '' | 'none' | 'low' | 'medium' | 'high' | 'xhigh';
    targetIdeaId: string;
  };

  let {
    focusCycleId = null,
    refreshSerial = null,
  }: {
    focusCycleId?: number | null;
    refreshSerial?: number | null;
  } = $props();

  const DEFAULT_TIME = '09:00';
  const DEFAULT_SCHEDULE = '0 9 * * *';
  const ONE_TIME_PREFIX = 'at:';
  const CADENCES: Array<{ value: Cadence; label: string }> = [
    { value: 'once', label: 'Once' },
    { value: 'daily', label: 'Daily' },
    { value: 'weekdays', label: 'Weekdays' },
    { value: 'weekly', label: 'Weekly' },
    { value: 'monthly', label: 'Monthly' },
    { value: 'custom', label: 'Custom' },
  ];
  const WEEKDAYS = [
    { value: '0', label: 'Sun' },
    { value: '1', label: 'Mon' },
    { value: '2', label: 'Tue' },
    { value: '3', label: 'Wed' },
    { value: '4', label: 'Thu' },
    { value: '5', label: 'Fri' },
    { value: '6', label: 'Sat' },
  ];
  const THINKING_LEVELS: Array<{ value: FormState['thinkingOverride']; label: string }> = [
    { value: '', label: 'Default' },
    { value: 'none', label: 'None' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'xhigh', label: 'XHigh' },
  ];

  let cycles = $state<CycleRead[]>([]);
  let runs = $state<CycleRunRead[]>([]);
  let loading = $state(true);
  let runsLoading = $state(false);
  let saving = $state(false);
  let selectedCycleId = $state<number | null>(null);
  let isDraft = $state(false);
  let lastFocusCycleId = $state<number | null>(null);
  let lastRefreshSerial = $state<number | null>(null);
  let behaviorPolicyRefreshSerial = $state(0);
  let unsubscribeCyclesChanged: (() => void) | null = null;

  const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const selectedCycle = $derived.by(
    () => cycles.find((cycle) => cycle.id === selectedCycleId) ?? null,
  );
  const activeCount = $derived.by(() => cycles.filter((cycle) => cycle.enabled).length);
  const schedulePreview = $derived.by(() => labelForForm(form));

  function pad2(value: number): string {
    return String(value).padStart(2, '0');
  }

  function dateInputFromDate(date: Date): string {
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  }

  function defaultRunDate(): string {
    return dateInputFromDate(new Date(Date.now() + 86400000));
  }

  function emptyForm(): FormState {
    return {
      name: '',
      prompt: '',
      cadence: 'once',
      date: defaultRunDate(),
      time: DEFAULT_TIME,
      weekday: '1',
      monthday: '1',
      customSchedule: DEFAULT_SCHEDULE,
      enabled: true,
      modelOverride: '',
      thinkingOverride: '',
      targetIdeaId: '',
    };
  }

  let form = $state<FormState>(emptyForm());

  function normalizeTime(value: string | null | undefined): string {
    const match = String(value ?? '').match(/^(\d{1,2}):(\d{2})$/);
    if (!match) return DEFAULT_TIME;
    const hour = Number(match[1]);
    const minute = Number(match[2]);
    if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return DEFAULT_TIME;
    return `${pad2(hour)}:${pad2(minute)}`;
  }

  function timeFromCron(minute: string, hour: string): string | null {
    if (!/^\d+$/.test(minute) || !/^\d+$/.test(hour)) return null;
    const h = Number(hour);
    const m = Number(minute);
    if (h < 0 || h > 23 || m < 0 || m > 59) return null;
    return `${pad2(h)}:${pad2(m)}`;
  }

  function isOneTimeSchedule(expr: string | null | undefined): boolean {
    return String(expr ?? '').trim().toLowerCase().startsWith(ONE_TIME_PREFIX);
  }

  function oneTimeDate(expr: string | null | undefined): Date | null {
    if (!isOneTimeSchedule(expr)) return null;
    const raw = String(expr ?? '').trim().slice(ONE_TIME_PREFIX.length).trim();
    if (!raw) return null;
    const date = new Date(raw);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function parseSchedule(cycle: CycleRead | null): Pick<FormState, 'cadence' | 'date' | 'time' | 'weekday' | 'monthday' | 'customSchedule'> {
    const expr = (cycle?.schedule_expr || DEFAULT_SCHEDULE).trim();
    const once = oneTimeDate(expr);
    if (once) {
      return {
        cadence: 'once',
        date: dateInputFromDate(once),
        time: normalizeTime(`${once.getHours()}:${pad2(once.getMinutes())}`),
        weekday: '1',
        monthday: '1',
        customSchedule: expr,
      };
    }

    const [minute, hour, dayOfMonth, month, dayOfWeek, ...extra] = expr.split(/\s+/);
    const time = timeFromCron(minute, hour);
    const base = {
      date: defaultRunDate(),
      time: time || DEFAULT_TIME,
      weekday: '1',
      monthday: '1',
      customSchedule: expr,
    };
    if (!time || !dayOfMonth || !month || !dayOfWeek || extra.length) return { ...base, cadence: 'custom' };
    if (dayOfMonth === '*' && month === '*' && dayOfWeek === '*') return { ...base, cadence: 'daily' };
    if (dayOfMonth === '*' && month === '*' && ['1-5', '1,2,3,4,5'].includes(dayOfWeek)) {
      return { ...base, cadence: 'weekdays' };
    }
    if (dayOfMonth === '*' && month === '*' && /^\d+$/.test(dayOfWeek)) {
      return { ...base, cadence: 'weekly', weekday: dayOfWeek === '7' ? '0' : dayOfWeek };
    }
    if (month === '*' && dayOfWeek === '*' && /^\d+$/.test(dayOfMonth)) {
      return { ...base, cadence: 'monthly', monthday: dayOfMonth };
    }
    return { ...base, cadence: 'custom' };
  }

  function fillForm(cycle: CycleRead | null) {
    if (!cycle) {
      form = emptyForm();
      return;
    }
    const schedule = parseSchedule(cycle);
    form = {
      name: cycle.name,
      prompt: cycle.prompt,
      ...schedule,
      enabled: cycle.enabled,
      modelOverride: cycle.model_override || '',
      thinkingOverride: (cycle.thinking_override as FormState['thinkingOverride']) || '',
      targetIdeaId: cycle.target_idea_id || '',
    };
  }

  function scheduleExprFromForm(): string {
    const time = normalizeTime(form.time);
    if (form.cadence === 'once') return `${ONE_TIME_PREFIX}${form.date || defaultRunDate()}T${time}:00`;
    if (form.cadence === 'custom') return form.customSchedule.trim();
    const [hour, minute] = time.split(':').map((value) => String(Number(value)));
    if (form.cadence === 'weekdays') return `${minute} ${hour} * * 1-5`;
    if (form.cadence === 'weekly') return `${minute} ${hour} * * ${form.weekday}`;
    if (form.cadence === 'monthly') return `${minute} ${hour} ${form.monthday} * *`;
    return `${minute} ${hour} * * *`;
  }

  function formatTime(value: string): string {
    const [hour, minute] = normalizeTime(value).split(':').map(Number);
    return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(
      new Date(2026, 0, 1, hour, minute),
    );
  }

  function formatDate(value: string): string {
    const [year, month, day] = value.split('-').map(Number);
    if (!year || !month || !day) return value || 'selected date';
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(year, month - 1, day));
  }

  function labelForForm(value: FormState): string {
    const time = formatTime(value.time);
    if (value.cadence === 'once') return `Once on ${formatDate(value.date)} at ${time}`;
    if (value.cadence === 'weekdays') return `Weekdays at ${time}`;
    if (value.cadence === 'weekly') return `${WEEKDAYS.find((day) => day.value === value.weekday)?.label ?? 'Weekly'} at ${time}`;
    if (value.cadence === 'monthly') return `Monthly on day ${value.monthday} at ${time}`;
    if (value.cadence === 'custom') return value.customSchedule || 'Custom schedule';
    return `Daily at ${time}`;
  }

  function labelForCycle(cycle: CycleRead): string {
    if (isOneTimeSchedule(cycle.schedule_expr)) {
      const parsed = parseSchedule(cycle);
      return labelForForm({ ...emptyForm(), ...parsed });
    }
    return cycle.schedule_human || labelForForm({ ...emptyForm(), ...parseSchedule(cycle) });
  }

  function statusTone(cycle: CycleRead): 'muted' | 'warning' | 'success' | 'danger' | 'info' {
    const status = String(cycle.last_status || '').toLowerCase();
    if (cycle.last_error || ['failed', 'error'].includes(status)) return 'danger';
    if (!cycle.enabled) return isOneTimeSchedule(cycle.schedule_expr) && cycle.last_run_at ? 'info' : 'muted';
    if (['running', 'queued', 'pending'].includes(status)) return 'warning';
    return 'success';
  }

  function statusLabel(cycle: CycleRead): string {
    if (cycle.last_error) return 'Needs work';
    if (!cycle.enabled && isOneTimeSchedule(cycle.schedule_expr) && cycle.last_run_at) return 'Done';
    if (!cycle.enabled) return 'Paused';
    return 'Active';
  }

  function formatDateTime(value: string | null | undefined): string {
    if (!value) return '--';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '--';
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
  }

  async function loadRuns(cycleId: number | null) {
    if (!cycleId) {
      runs = [];
      return;
    }
    runsLoading = true;
    try {
      runs = await api.listCycleRuns(cycleId, 8);
    } catch {
      runs = [];
    } finally {
      runsLoading = false;
    }
  }

  async function loadCycles(preferredCycleId: number | null = selectedCycleId) {
    loading = true;
    try {
      const nextCycles = await api.listCycles();
      cycles = nextCycles;
      const nextSelected = preferredCycleId
        ? nextCycles.find((cycle) => cycle.id === preferredCycleId) ?? null
        : selectedCycle ?? nextCycles[0] ?? null;
      selectedCycleId = nextSelected?.id ?? null;
      isDraft = !nextSelected && isDraft;
      if (nextSelected) fillForm(nextSelected);
      await loadRuns(nextSelected?.id ?? null);
      behaviorPolicyRefreshSerial += 1;
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to load cycles', 'error');
    } finally {
      loading = false;
    }
  }

  async function selectCycle(cycle: CycleRead) {
    selectedCycleId = cycle.id;
    isDraft = false;
    fillForm(cycle);
    await loadRuns(cycle.id);
  }

  function newCycle() {
    selectedCycleId = null;
    isDraft = true;
    runs = [];
    fillForm(null);
  }

  async function saveCycle() {
    const scheduleExpr = scheduleExprFromForm();
    const wasUpdate = Boolean(selectedCycleId);
    const payload = {
      name: form.name.trim(),
      prompt: form.prompt.trim(),
      schedule_expr: scheduleExpr,
      timezone: localTimezone,
      enabled: form.enabled,
      model_override: form.modelOverride.trim() || null,
      thinking_override: form.thinkingOverride || null,
      execution_mode: 'reuse_same_idea' as const,
      target_idea_id: form.targetIdeaId || selectedCycle?.target_idea_id || null,
      reopen_archived: true,
    };
    if (!payload.name || !payload.prompt || !payload.schedule_expr) {
      ui.toast('Name, prompt, and schedule are required', 'error');
      return;
    }
    saving = true;
    try {
      const saved = selectedCycleId
        ? await api.updateCycle(selectedCycleId, payload)
        : await api.createCycle(payload);
      selectedCycleId = saved.id;
      isDraft = false;
      fillForm(saved);
      ui.toast(wasUpdate ? 'Cycle saved' : 'Cycle created', 'success');
      await loadCycles(saved.id);
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to save cycle', 'error');
    } finally {
      saving = false;
    }
  }

  async function runSelectedNow() {
    if (!selectedCycleId) return;
    try {
      await api.runCycle(selectedCycleId);
      ui.toast('Cycle launched', 'success');
      await loadCycles(selectedCycleId);
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to run cycle', 'error');
    }
  }

  async function deleteSelected() {
    if (!selectedCycleId || !selectedCycle) return;
    if (!window.confirm(`Delete cycle "${selectedCycle.name}"?`)) return;
    try {
      await api.deleteCycle(selectedCycleId);
      ui.toast('Cycle deleted', 'success');
      selectedCycleId = null;
      isDraft = false;
      await loadCycles(null);
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to delete cycle', 'error');
    }
  }

  $effect(() => {
    if (!focusCycleId || focusCycleId === lastFocusCycleId) return;
    lastFocusCycleId = focusCycleId;
    void loadCycles(focusCycleId);
  });

  $effect(() => {
    if (!refreshSerial || refreshSerial === lastRefreshSerial) return;
    lastRefreshSerial = refreshSerial;
    void loadCycles(focusCycleId || selectedCycleId);
  });

  onMount(() => {
    void loadCycles(focusCycleId);
    unsubscribeCyclesChanged = wsClient.on('cycles_changed', (msg) => {
      const cycleId = Number(msg?.cycle_id || 0) || selectedCycleId;
      void loadCycles(cycleId ?? null);
    });
  });

  onDestroy(() => {
    unsubscribeCyclesChanged?.();
  });
</script>

<section class="thread-cycles-pane" aria-label="Cycles">
  <header class="cycles-pane-header">
    <div>
      <span>Cycles</span>
      <strong>{cycles.length} total / {activeCount} active</strong>
    </div>
    <ConstellationButton variant="quiet" size="sm" onclick={newCycle}>
      New
    </ConstellationButton>
  </header>

  {#if loading && cycles.length === 0}
    <div class="cycle-pane-loading">
      <span></span>
      <span></span>
      <span></span>
    </div>
  {:else if cycles.length > 0}
    <div class="cycle-pane-list" aria-label="Cycle list">
      {#each cycles as cycle (cycle.id)}
        <button
          type="button"
          class="cycle-pane-row"
          class:is-selected={!isDraft && selectedCycleId === cycle.id}
          onclick={() => selectCycle(cycle)}
        >
          <span>
            <strong>{cycle.name}</strong>
            <small>{labelForCycle(cycle)}</small>
          </span>
          <ConstellationPill variant={statusTone(cycle)}>{statusLabel(cycle)}</ConstellationPill>
        </button>
      {/each}
    </div>
  {:else if !isDraft}
    <button type="button" class="cycle-pane-empty" onclick={newCycle}>
      <ConstellationIcon name="cycles" size={18} stroke={1.8} />
      <span>Create the first cycle</span>
    </button>
  {/if}

  {#if isDraft || selectedCycle}
    {#if selectedCycle}
      <EffectiveCyclePolicyView
        cycleId={selectedCycle.id}
        compact
        refreshSerial={behaviorPolicyRefreshSerial}
      />
    {/if}
    <div class="cycle-pane-editor">
      <div class="cycle-pane-editor-head">
        <span>{isDraft ? 'New cycle' : 'Selected cycle'}</span>
        <strong>{schedulePreview}</strong>
      </div>

      <label class="cycle-pane-field">
        <span>Name</span>
        <input bind:value={form.name} placeholder="Reminder" />
      </label>

      <label class="cycle-pane-field">
        <span>Prompt</span>
        <textarea bind:value={form.prompt} rows="5" placeholder="Remind me to..."></textarea>
      </label>

      <div class="cycle-pane-cadence" role="group" aria-label="Schedule frequency">
        {#each CADENCES as cadence}
          <button
            type="button"
            class:active={form.cadence === cadence.value}
            onclick={() => (form.cadence = cadence.value)}
          >
            {cadence.label}
          </button>
        {/each}
      </div>

      {#if form.cadence === 'custom'}
        <label class="cycle-pane-field">
          <span>Schedule</span>
          <input bind:value={form.customSchedule} class="mono" placeholder="0 9 * * *" />
        </label>
      {:else}
        <div class="cycle-pane-schedule-grid">
          {#if form.cadence === 'once'}
            <label class="cycle-pane-field">
              <span>Date</span>
              <input bind:value={form.date} type="date" />
            </label>
          {/if}
          <label class="cycle-pane-field">
            <span>Time</span>
            <input bind:value={form.time} type="time" />
          </label>
          {#if form.cadence === 'weekly'}
            <label class="cycle-pane-field">
              <span>Day</span>
              <select bind:value={form.weekday}>
                {#each WEEKDAYS as day}
                  <option value={day.value}>{day.label}</option>
                {/each}
              </select>
            </label>
          {:else if form.cadence === 'monthly'}
            <label class="cycle-pane-field">
              <span>Day</span>
              <select bind:value={form.monthday}>
                {#each Array.from({ length: 31 }, (_, index) => String(index + 1)) as day}
                  <option value={day}>{day}</option>
                {/each}
              </select>
            </label>
          {/if}
        </div>
      {/if}

      <div class="cycle-pane-switch-row">
        <button
          type="button"
          class="cycle-pane-switch"
          class:active={form.enabled}
          aria-pressed={form.enabled}
          onclick={() => (form.enabled = !form.enabled)}
        >
          <span aria-hidden="true"></span>
          {form.enabled ? 'Active' : 'Paused'}
        </button>
        <label class="cycle-pane-field">
          <span>Reasoning</span>
          <select bind:value={form.thinkingOverride}>
            {#each THINKING_LEVELS as level}
              <option value={level.value}>{level.label}</option>
            {/each}
          </select>
        </label>
      </div>

      <div class="cycle-pane-actions">
        <ConstellationButton variant="primary" size="sm" loading={saving} onclick={saveCycle}>
          Save
        </ConstellationButton>
        {#if selectedCycleId}
          <ConstellationButton variant="quiet" size="sm" onclick={runSelectedNow}>Run now</ConstellationButton>
          <ConstellationButton variant="destructive" size="sm" onclick={deleteSelected}>Delete</ConstellationButton>
        {/if}
      </div>

      {#if selectedCycle}
        <div class="cycle-pane-runs">
          <span>Recent runs</span>
          {#if runsLoading}
            <small>Loading...</small>
          {:else if runs.length === 0}
            <small>No runs yet.</small>
          {:else}
            {#each runs as run (run.id)}
              <article>
                <strong>{formatDateTime(run.scheduled_for)}</strong>
                <ConstellationPill variant={run.status === 'failed' ? 'danger' : run.status === 'completed' ? 'success' : 'muted'}>
                  {run.status}
                </ConstellationPill>
              </article>
            {/each}
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</section>

<style>
  .thread-cycles-pane {
    display: grid;
    align-content: start;
    gap: 12px;
    width: 100%;
    min-height: 0;
    color: var(--constellation-color-text-primary);
  }

  .cycles-pane-header,
  .cycle-pane-editor-head,
  .cycle-pane-actions,
  .cycle-pane-switch-row,
  .cycle-pane-runs article {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .cycles-pane-header {
    padding: 2px 2px 10px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .cycles-pane-header div,
  .cycle-pane-editor-head {
    display: grid;
    min-width: 0;
    gap: 4px;
  }

  .cycles-pane-header span,
  .cycle-pane-editor-head span,
  .cycle-pane-field span,
  .cycle-pane-runs > span {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.12em;
    line-height: 1.2;
    text-transform: uppercase;
  }

  .cycles-pane-header strong,
  .cycle-pane-editor-head strong {
    min-width: 0;
    overflow: hidden;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    font-weight: 520;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cycle-pane-list,
  .cycle-pane-editor,
  .cycle-pane-loading,
  .cycle-pane-runs {
    display: grid;
    gap: 8px;
  }

  .cycle-pane-row,
  .cycle-pane-empty {
    width: 100%;
    min-width: 0;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
    color: inherit;
    cursor: pointer;
  }

  .cycle-pane-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 8px;
    padding: 10px;
    text-align: left;
  }

  .cycle-pane-row:hover,
  .cycle-pane-row.is-selected {
    border-color: var(--constellation-control-focus-ring);
    background: var(--constellation-control-button-secondary-background-hover);
  }

  .cycle-pane-row span {
    display: grid;
    min-width: 0;
    gap: 4px;
  }

  .cycle-pane-row strong,
  .cycle-pane-row small {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cycle-pane-row strong {
    font-size: 12px;
    font-weight: 620;
  }

  .cycle-pane-row small,
  .cycle-pane-runs small {
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    line-height: 1.35;
  }

  .cycle-pane-empty {
    display: flex;
    min-height: 86px;
    align-items: center;
    justify-content: center;
    gap: 8px;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .cycle-pane-editor {
    padding-top: 4px;
  }

  .cycle-pane-field {
    display: grid;
    gap: 6px;
    min-width: 0;
  }

  .cycle-pane-field input,
  .cycle-pane-field textarea,
  .cycle-pane-field select {
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
    border: 1px solid var(--constellation-control-field-border);
    border-radius: 8px;
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-primary);
    font: inherit;
    font-size: 12px;
    line-height: 1.45;
  }

  .cycle-pane-field input,
  .cycle-pane-field select {
    min-height: 36px;
    padding: 8px 10px;
  }

  .cycle-pane-field textarea {
    min-height: 104px;
    resize: vertical;
    padding: 9px 10px;
  }

  .cycle-pane-field .mono {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
  }

  .cycle-pane-field input:focus,
  .cycle-pane-field textarea:focus,
  .cycle-pane-field select:focus,
  .cycle-pane-cadence button:focus-visible,
  .cycle-pane-row:focus-visible,
  .cycle-pane-empty:focus-visible,
  .cycle-pane-switch:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .cycle-pane-cadence {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }

  .cycle-pane-cadence button {
    min-height: 32px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    cursor: pointer;
  }

  .cycle-pane-cadence button.active,
  .cycle-pane-cadence button:hover {
    border-color: var(--constellation-control-focus-ring);
    color: var(--constellation-color-text-primary);
  }

  .cycle-pane-schedule-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .cycle-pane-switch-row {
    align-items: end;
  }

  .cycle-pane-switch {
    display: inline-flex;
    min-height: 36px;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
    color: var(--constellation-color-text-primary);
    padding: 0 10px;
    font-size: 12px;
    cursor: pointer;
  }

  .cycle-pane-switch > span {
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: var(--constellation-color-text-muted);
  }

  .cycle-pane-switch.active > span {
    background: var(--constellation-color-success);
  }

  .cycle-pane-actions {
    justify-content: flex-start;
    flex-wrap: wrap;
  }

  .cycle-pane-runs {
    border-top: 1px solid var(--constellation-surface-panel-separator);
    padding-top: 10px;
  }

  .cycle-pane-runs article {
    min-height: 32px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
    font-size: 11px;
  }

  .cycle-pane-runs article:last-child {
    border-bottom: 0;
  }

  .cycle-pane-loading span {
    height: 44px;
    border-radius: 8px;
    background:
      linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.06), transparent),
      var(--constellation-surface-nested-background);
    background-size: 200% 100%;
    animation: cycle-pane-pulse 1.4s ease-in-out infinite;
  }

  @keyframes cycle-pane-pulse {
    from {
      background-position: 200% 0;
    }
    to {
      background-position: -200% 0;
    }
  }

  @media (max-width: 560px) {
    .cycle-pane-cadence,
    .cycle-pane-schedule-grid {
      grid-template-columns: 1fr;
    }

    .cycle-pane-switch-row {
      display: grid;
    }
  }
</style>
