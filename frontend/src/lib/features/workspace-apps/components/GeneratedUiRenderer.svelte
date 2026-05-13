<script lang="ts">
  import { onDestroy } from 'svelte';

  import {
    createDomainRecord,
    listDomainRecords,
    runWorkspaceAppAction,
    updateDomainRecord,
    type DomainRecordRead,
    type WorkspaceAppRead,
  } from '$lib/features/workspace-apps/api/workspaceAppsApi';
  import { ConstellationIcon } from '$lib/components/constellation';
  import { workspaceApps } from '$lib/stores/workspaceApps.svelte';
  import {
    bindingAllowsField,
    bindingAllowsOperation,
  } from '$lib/utils/generatedWorkspaceAppContract';

  type GeneratedUiColumn = {
    key: string;
    label?: string;
    type?: string;
    editable?: boolean;
    options?: Array<string | { label?: string; value?: any }>;
    width?: string;
  };
  type GeneratedUiColumnInput = GeneratedUiColumn | string;

  type GeneratedUiBoardGroup = string | { label?: string; value?: any };
  type GeneratedUiBoardCard = {
    title?: string;
    subtitle?: string;
    description?: string;
    badges?: string[];
    meta?: string[];
    assignee?: string;
    href?: string;
  };

  type GeneratedUiAction = string | {
    key?: string;
    action_key?: string;
    label?: string;
    title?: string;
    description?: string;
    payload?: Record<string, any>;
    disabled?: boolean;
    hidden?: boolean;
  };

  type NormalizedGeneratedUiAction = {
    key: string;
    label: string;
    description?: string;
    payload: Record<string, any>;
    disabled: boolean;
  };

  type GeneratedUiView = {
    id?: string;
    type?: string;
    title?: string;
    description?: string;
    binding?: string;
    data_binding?: string;
    object_key?: string;
    columns?: GeneratedUiColumnInput[];
    fields?: GeneratedUiColumnInput[];
    groups?: GeneratedUiBoardGroup[];
    board_columns?: GeneratedUiBoardGroup[];
    boardColumns?: GeneratedUiBoardGroup[];
    groupBy?: string;
    rows?: Record<string, any>[];
    records?: Record<string, any>[];
    metrics?: Array<{ key?: string; label?: string; value?: any; op?: string; field?: string; equals?: any }>;
    chart_type?: string;
    chart?: string;
    group_by?: string;
    card?: GeneratedUiBoardCard;
    allow_create?: boolean;
    create?: boolean | { fields?: GeneratedUiColumnInput[] };
    empty_state?: string;
  };

  type GeneratedUiSpec = {
    schema_version?: number | string;
    version?: number | string;
    title?: string;
    description?: string;
    primary_binding?: string;
    actions?: GeneratedUiAction[];
    views?: GeneratedUiView[];
    rows?: Record<string, any>[];
    records?: Record<string, any>[];
    data?: { rows?: Record<string, any>[]; records?: Record<string, any>[] };
  };

  type DomainBinding = {
    domain_id?: number;
    domainId?: number;
    domain_slug?: string;
    object_key?: string;
    objectKey?: string;
    operations?: string[];
    fields?: string[];
  };

  let {
    app,
    surface = 'workspace',
    onclose,
  }: {
    app: WorkspaceAppRead;
    surface?: 'workspace' | 'dock';
    onclose?: () => void;
  } = $props();

  let search = $state('');
  let recordsByAlias = $state<Record<string, DomainRecordRead[]>>({});
  let loadingRecords = $state(false);
  let loadError = $state<string | null>(null);
  let lastLoadSignature = $state('');
  let draftByView = $state<Record<string, Record<string, any>>>({});
  let busyCell = $state<Record<string, boolean>>({});
  let busyCreate = $state<Record<string, boolean>>({});
  let busyAction = $state<Record<string, boolean>>({});
  let actionNotice = $state<string | null>(null);
  let actionError = $state<string | null>(null);
  let uiStateBase = $state<Record<string, any>>({});
  let uiStateLoadSignature = $state('');
  let uiStateLoaded = $state(false);
  let persistTimer: ReturnType<typeof setTimeout> | null = null;

  const activeVersion = $derived(app.active_version);
  const manifest = $derived(activeVersion?.manifest ?? {});
  const stateKey = $derived(String(manifest.state_key || 'default'));
  const parsedSpec = $derived(parseSpec(activeVersion?.source_code || ''));
  const spec = $derived(parsedSpec.spec);
  const domainBindings = $derived(extractDomainBindings(manifest));
  const primaryBindingAlias = $derived(
    spec?.primary_binding || Object.keys(domainBindings)[0] || null,
  );
  const views = $derived(normalizeViews(spec));
  const hasBoardView = $derived(views.some((view) => view.type === 'board'));
  const structuredActions = $derived(generatedActionsForSpec(spec, manifest));
  const bindingSignature = $derived(
    JSON.stringify(Object.entries(domainBindings).map(([alias, binding]) => [
      alias,
      binding.domain_id ?? binding.domainId,
      binding.object_key ?? binding.objectKey,
    ])),
  );

  $effect(() => {
    const signature = `${app.id}:${activeVersion?.id ?? 'none'}:${bindingSignature}`;
    if (signature === lastLoadSignature) return;
    lastLoadSignature = signature;
    void loadBoundRecords();
  });

  $effect(() => {
    const signature = `${app.id}:${stateKey}`;
    if (signature === uiStateLoadSignature) return;
    uiStateLoadSignature = signature;
    void loadPersistedUiState();
  });

  onDestroy(() => {
    if (persistTimer) clearTimeout(persistTimer);
  });

  function isRecord(value: unknown): value is Record<string, any> {
    return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
  }

  function parseSpec(source: string): { spec: GeneratedUiSpec | null; error: string | null } {
    if (!source.trim()) return { spec: null, error: 'No generated UI spec was saved for this app.' };
    try {
      const parsed = JSON.parse(source);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        return { spec: null, error: 'Generated UI spec must be a JSON object.' };
      }
      return { spec: parsed as GeneratedUiSpec, error: null };
    } catch (err: any) {
      return { spec: null, error: err?.message || 'Generated UI spec could not be parsed.' };
    }
  }

  function extractDomainBindings(appManifest: Record<string, any>): Record<string, DomainBinding> {
    const bindings = appManifest?.data_plan?.bindings;
    if (!bindings || typeof bindings !== 'object' || Array.isArray(bindings)) return {};
    return bindings as Record<string, DomainBinding>;
  }

  function normalizeViews(source: GeneratedUiSpec | null): GeneratedUiView[] {
    if (!source) return [];
    if (Array.isArray(source.views) && source.views.length) return source.views;
    return [{ id: 'records', type: 'table', title: source.title || app.name }];
  }

  function manifestActionDeclarations(appManifest: Record<string, any>): Record<string, any> {
    const direct = appManifest.actions;
    if (isRecord(direct)) return direct;
    const actionPlan = appManifest.action_plan;
    if (isRecord(actionPlan) && isRecord(actionPlan.actions)) return actionPlan.actions;
    return {};
  }

  function generatedActionsForSpec(
    source: GeneratedUiSpec | null,
    appManifest: Record<string, any>,
  ): NormalizedGeneratedUiAction[] {
    const declarations = manifestActionDeclarations(appManifest);
    const configured = Array.isArray(source?.actions) && source.actions.length
      ? source.actions
      : Object.keys(declarations);
    return configured
      .map((raw) => normalizeGeneratedAction(raw, declarations))
      .filter((action): action is NormalizedGeneratedUiAction => Boolean(action));
  }

  function normalizeGeneratedAction(
    raw: GeneratedUiAction,
    declarations: Record<string, any>,
  ): NormalizedGeneratedUiAction | null {
    const rawObject = isRecord(raw) ? raw : null;
    const key = String(
      typeof raw === 'string' ? raw : rawObject?.key || rawObject?.action_key || '',
    ).trim();
    if (!key) return null;
    const declaration = declarations[key];
    if (!isRecord(declaration)) return null;
    const ui = isRecord(declaration.ui) ? declaration.ui : {};
    if (rawObject?.hidden || ui.hidden === true) return null;
    const label = String(rawObject?.label || rawObject?.title || ui.label || declaration.label || actionLabelFromKey(key)).trim();
    return {
      key,
      label: label || actionLabelFromKey(key),
      description: String(rawObject?.description || declaration.description || '').trim() || undefined,
      payload: isRecord(rawObject?.payload) ? rawObject.payload : {},
      disabled: rawObject?.disabled === true || ui.disabled === true,
    };
  }

  function actionLabelFromKey(key: string): string {
    const parts = key.split('.');
    const tail = parts[parts.length - 1] || key;
    return labelFromKey(tail.replace(/([a-z])([A-Z])/g, '$1 $2'));
  }

  function viewId(view: GeneratedUiView, index: number): string {
    return view.id || `${view.type || 'view'}-${index}`;
  }

  function bindingAliasForView(view: GeneratedUiView): string | null {
    return view.binding || view.data_binding || primaryBindingAlias;
  }

  function bindingForView(view: GeneratedUiView): DomainBinding | null {
    const alias = bindingAliasForView(view);
    return alias ? domainBindings[alias] ?? null : null;
  }

  async function loadPersistedUiState() {
    uiStateLoaded = false;
    try {
      const saved = (await workspaceApps.loadState(app.id, stateKey, { silent: true })) ?? {};
      uiStateBase = saved;
      const generatedUiState = isRecord(saved.generated_ui) ? saved.generated_ui : {};
      search = typeof generatedUiState.search === 'string' ? generatedUiState.search : '';
      draftByView = isRecord(generatedUiState.drafts) ? generatedUiState.drafts : {};
    } finally {
      uiStateLoaded = true;
    }
  }

  function schedulePersistUiState() {
    if (!uiStateLoaded) return;
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(() => {
      persistTimer = null;
      void persistUiState();
    }, 450);
  }

  async function persistUiState() {
    const existingGeneratedUiState = isRecord(uiStateBase.generated_ui) ? uiStateBase.generated_ui : {};
    const nextState = {
      ...uiStateBase,
      generated_ui: {
        ...existingGeneratedUiState,
        search,
        drafts: draftByView,
      },
    };
    try {
      uiStateBase = await workspaceApps.updateState(app.id, stateKey, nextState);
    } catch {
      // UI preferences are helpful, but failed persistence should not block Domain work.
    }
  }

  function setSearch(value: string) {
    search = value;
    schedulePersistUiState();
  }

  async function loadBoundRecords() {
    const entries = Object.entries(domainBindings);
    if (!entries.length) {
      recordsByAlias = {};
      loadError = null;
      return;
    }

    loadingRecords = true;
    loadError = null;
    try {
      const nextRecords: Record<string, DomainRecordRead[]> = {};
      for (const [alias, binding] of entries) {
        const domainId = Number(binding.domain_id ?? binding.domainId);
        if (!Number.isFinite(domainId) || domainId <= 0) continue;
        nextRecords[alias] = await listDomainRecords(domainId, {
          objectKey: binding.object_key ?? binding.objectKey,
          limit: 200,
        });
      }
      recordsByAlias = nextRecords;
    } catch (err: any) {
      loadError = err?.detail || 'Failed to load Domain records.';
    } finally {
      loadingRecords = false;
    }
  }

  function staticRowsForView(view: GeneratedUiView): Record<string, any>[] {
    const candidates = [
      view.rows,
      view.records,
      spec?.rows,
      spec?.records,
      spec?.data?.rows,
      spec?.data?.records,
    ];
    return (candidates.find((candidate) => Array.isArray(candidate)) ?? []) as Record<string, any>[];
  }

  function rowsForView(view: GeneratedUiView): Record<string, any>[] {
    const alias = bindingAliasForView(view);
    const boundRecords = alias ? recordsByAlias[alias] : null;
    if (boundRecords) {
      return boundRecords.map((record) => ({
        id: record.id,
        title: record.title,
        version: record.version,
        object_key: record.object_key,
        ...record.data,
        __record: record,
      }));
    }
    return staticRowsForView(view);
  }

  function filterRows(rows: Record<string, any>[]): Record<string, any>[] {
    const term = search.trim().toLowerCase();
    if (!term) return rows;
    return rows.filter((row) => JSON.stringify(row).toLowerCase().includes(term));
  }

  function columnsForView(view: GeneratedUiView, rows: Record<string, any>[]): GeneratedUiColumn[] {
    const configured = view.columns || view.fields;
    if (Array.isArray(configured) && configured.length) {
      return configured.map(normalizeColumn).filter((column): column is GeneratedUiColumn => !!column?.key);
    }
    const bindingFields = bindingForView(view)?.fields;
    if (Array.isArray(bindingFields) && bindingFields.length) {
      return bindingFields
        .filter((key) => typeof key === 'string' && key.trim())
        .slice(0, 8)
        .map((key) => ({ key, label: labelFromKey(key) }));
    }
    const first = rows[0] || {};
    return Object.keys(first)
      .filter((key) => !key.startsWith('__') && !['id', 'version', 'object_key'].includes(key))
      .slice(0, 6)
      .map((key) => ({ key, label: labelFromKey(key) }));
  }

  function createFieldsForView(view: GeneratedUiView, rows: Record<string, any>[]): GeneratedUiColumn[] {
    const binding = bindingForView(view);
    if (view.create && typeof view.create === 'object' && Array.isArray(view.create.fields)) {
      return view.create.fields
        .map(normalizeColumn)
        .filter((field): field is GeneratedUiColumn => !!field?.key && bindingAllowsField(binding, field.key));
    }
    return columnsForView(view, rows).filter((field) => (
      !['id', 'version'].includes(field.key)
      && bindingAllowsField(binding, field.key)
    ));
  }

  function labelFromKey(key: string): string {
    return key
      .replace(/^_+/, '')
      .replace(/[_-]+/g, ' ')
      .replace(/\b\w/g, (char) => char.toUpperCase());
  }

  function normalizeColumn(raw: GeneratedUiColumnInput): GeneratedUiColumn | null {
    if (typeof raw === 'string') {
      const key = raw.trim();
      return key ? { key, label: labelFromKey(key) } : null;
    }
    if (!raw || typeof raw !== 'object') return null;
    const key = String(raw.key || '').trim();
    return key ? { ...raw, key, label: raw.label || labelFromKey(key) } : null;
  }

  function cellValue(row: Record<string, any>, column: GeneratedUiColumn): any {
    if (column.key === 'title') return row.title;
    return row[column.key];
  }

  function formatValue(value: any, column?: GeneratedUiColumn): string {
    if (value === null || value === undefined || value === '') return '—';
    if (column?.type === 'boolean') return value ? 'Yes' : 'No';
    if (Array.isArray(value)) return value.join(', ');
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }

  function optionValue(option: string | { label?: string; value?: any }) {
    return typeof option === 'string' ? option : option.value;
  }

  function optionLabel(option: string | { label?: string; value?: any }) {
    return typeof option === 'string' ? option : option.label || String(option.value ?? '');
  }

  function editableOptions(column: GeneratedUiColumn): Array<string | { label?: string; value?: any }> {
    if (Array.isArray(column.options) && column.options.length) return column.options;
    if (column.type === 'boolean') return [
      { label: 'No', value: false },
      { label: 'Yes', value: true },
    ];
    return [];
  }

  function canEditColumn(view: GeneratedUiView, column: GeneratedUiColumn): boolean {
    const binding = bindingForView(view);
    return (
      bindingAllowsOperation(binding, 'update')
      && bindingAllowsField(binding, column.key)
      && (column.editable === true || ['status', 'select', 'boolean'].includes(String(column.type || '')))
      && editableOptions(column).length > 0
    );
  }

  function coerceOptionValue(rawValue: string, column: GeneratedUiColumn): any {
    if (column.type === 'boolean') return rawValue === 'true';
    return rawValue;
  }

  async function updateCell(view: GeneratedUiView, row: Record<string, any>, column: GeneratedUiColumn, rawValue: string) {
    const alias = bindingAliasForView(view);
    const binding = alias ? domainBindings[alias] : null;
    const record = row.__record as DomainRecordRead | undefined;
    const domainId = Number(binding?.domain_id ?? binding?.domainId);
    if (!alias || !record || !Number.isFinite(domainId)) return;

    const cacheKey = `${alias}:${record.id}:${column.key}`;
    busyCell = { ...busyCell, [cacheKey]: true };
    try {
      const value = coerceOptionValue(rawValue, column);
      const payload = column.key === 'title'
        ? { title: String(value ?? ''), expected_version: record.version }
        : { data_patch: { [column.key]: value }, expected_version: record.version };
      const updated = await updateDomainRecord(domainId, record.id, payload);
      recordsByAlias = {
        ...recordsByAlias,
        [alias]: (recordsByAlias[alias] || []).map((candidate) => (
          candidate.id === updated.id ? updated : candidate
        )),
      };
    } finally {
      const { [cacheKey]: _removed, ...nextBusy } = busyCell;
      busyCell = nextBusy;
    }
  }

  function boardGroupKey(view: GeneratedUiView): string {
    return String(view.group_by || view.groupBy || 'status').trim() || 'status';
  }

  function normalizeBoardGroup(group: GeneratedUiBoardGroup): { label: string; value: any } {
    if (typeof group === 'string') return { label: group, value: group };
    const value = group?.value ?? group?.label ?? '';
    return { label: group?.label || formatValue(value), value };
  }

  function boardGroups(view: GeneratedUiView, rows: Record<string, any>[]): Array<{ label: string; value: any }> {
    const configured = view.groups || view.board_columns || view.boardColumns;
    if (Array.isArray(configured) && configured.length) {
      return configured.map(normalizeBoardGroup).filter((group) => String(group.value ?? '').trim());
    }
    const key = boardGroupKey(view);
    const values = Array.from(new Set(rows.map((row) => row[key]).filter((value) => value !== null && value !== undefined && value !== '')));
    if (values.length) return values.map((value) => ({ label: formatValue(value), value }));
    return ['Backlog', 'Todo', 'In Progress', 'In Review', 'Done'].map((value) => ({ label: value, value }));
  }

  function rowsForBoardGroup(rows: Record<string, any>[], groupKey: string, groupValue: any): Record<string, any>[] {
    return rows.filter((row) => String(row[groupKey] ?? '') === String(groupValue ?? ''));
  }

  function boardCard(view: GeneratedUiView): GeneratedUiBoardCard {
    return view.card || {};
  }

  function boardCardTitle(view: GeneratedUiView, row: Record<string, any>, columns: GeneratedUiColumn[]): string {
    const card = boardCard(view);
    const key = card.title || 'title';
    return formatValue(row[key] ?? row.title ?? row.name ?? row[columns[0]?.key]);
  }

  function boardCardSubtitle(view: GeneratedUiView, row: Record<string, any>): string {
    const key = boardCard(view).subtitle;
    return key ? formatValue(row[key]) : '';
  }

  function boardCardBadgeKeys(view: GeneratedUiView, columns: GeneratedUiColumn[]): string[] {
    const configured = boardCard(view).badges;
    if (Array.isArray(configured) && configured.length) return configured;
    const preferred = ['priority', 'status', 'state', 'milestone', 'labels', 'label'];
    const keys = columns.map((column) => column.key);
    return preferred.filter((key) => keys.includes(key)).slice(0, 3);
  }

  function boardRowId(row: Record<string, any>): string {
    return String(row.__record?.id ?? row.id ?? row.title ?? JSON.stringify(row));
  }

  function canMoveBoardCard(view: GeneratedUiView, groupKey: string, row: Record<string, any>): boolean {
    const binding = bindingForView(view);
    return Boolean(
      row.__record
      && bindingAllowsOperation(binding, 'update')
      && bindingAllowsField(binding, groupKey)
    );
  }

  function startBoardDrag(event: DragEvent, view: GeneratedUiView, index: number, row: Record<string, any>, groupKey: string) {
    if (!event.dataTransfer || !canMoveBoardCard(view, groupKey, row)) return;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('application/x-illo-board-card', JSON.stringify({
      viewId: viewId(view, index),
      rowId: boardRowId(row),
      groupKey,
    }));
  }

  async function dropBoardCard(
    event: DragEvent,
    view: GeneratedUiView,
    index: number,
    rows: Record<string, any>[],
    nextGroupValue: any,
  ) {
    event.preventDefault();
    const raw = event.dataTransfer?.getData('application/x-illo-board-card');
    if (!raw) return;
    try {
      const payload = JSON.parse(raw);
      if (payload.viewId !== viewId(view, index)) return;
      const row = rows.find((candidate) => boardRowId(candidate) === payload.rowId);
      const groupKey = String(payload.groupKey || boardGroupKey(view));
      if (!row || !canMoveBoardCard(view, groupKey, row)) return;
      await updateCell(view, row, {
        key: groupKey,
        label: labelFromKey(groupKey),
        type: typeof nextGroupValue === 'boolean' ? 'boolean' : 'status',
        editable: true,
        options: boardGroups(view, rows),
      }, String(nextGroupValue ?? ''));
    } catch {
      return;
    }
  }

  async function runGeneratedAction(action: NormalizedGeneratedUiAction) {
    if (busyAction[action.key] || action.disabled) return;
    busyAction = { ...busyAction, [action.key]: true };
    actionNotice = null;
    actionError = null;
    try {
      const result = await runWorkspaceAppAction(app.id, {
        action_key: action.key,
        payload: action.payload,
      });
      actionNotice = result.status === 'completed'
        ? `${action.label} completed.`
        : `${action.label}: ${result.status}`;
      await loadBoundRecords();
    } catch (err: any) {
      actionError = err?.detail || err?.message || 'Workspace action failed.';
    } finally {
      busyAction = { ...busyAction, [action.key]: false };
    }
  }

  async function createRecord(view: GeneratedUiView, index: number) {
    const alias = bindingAliasForView(view);
    const binding = bindingForView(view);
    const domainId = Number(binding?.domain_id ?? binding?.domainId);
    const objectKey = binding?.object_key ?? binding?.objectKey;
    if (!alias || !objectKey || !Number.isFinite(domainId) || !bindingAllowsOperation(binding, 'create')) return;

    const id = viewId(view, index);
    const draft = draftByView[id] || {};
    busyCreate = { ...busyCreate, [id]: true };
    try {
      const { title, ...data } = draft;
      const created = await createDomainRecord(domainId, objectKey, {
        data,
        title: title || undefined,
      });
      recordsByAlias = {
        ...recordsByAlias,
        [alias]: [created, ...(recordsByAlias[alias] || [])],
      };
      draftByView = { ...draftByView, [id]: {} };
      schedulePersistUiState();
    } finally {
      const { [id]: _removed, ...nextBusy } = busyCreate;
      busyCreate = nextBusy;
    }
  }

  function setDraftValue(view: GeneratedUiView, index: number, key: string, value: string) {
    const id = viewId(view, index);
    draftByView = {
      ...draftByView,
      [id]: {
        ...(draftByView[id] || {}),
        [key]: value,
      },
    };
    schedulePersistUiState();
  }

  function wantsCreate(view: GeneratedUiView): boolean {
    return view.type === 'form' || view.allow_create === true || !!view.create;
  }

  function canCreateRecords(view: GeneratedUiView): boolean {
    const binding = bindingForView(view);
    const domainId = Number(binding?.domain_id ?? binding?.domainId);
    const objectKey = String(binding?.object_key ?? binding?.objectKey ?? '').trim();
    return Boolean(
      Number.isFinite(domainId)
      && domainId > 0
      && objectKey
      && bindingAllowsOperation(binding, 'create'),
    );
  }

  function shouldShowCreate(view: GeneratedUiView): boolean {
    return wantsCreate(view) && canCreateRecords(view);
  }

  function metricValue(metric: NonNullable<GeneratedUiView['metrics']>[number], rows: Record<string, any>[]): string {
    if (metric.value !== undefined) return formatValue(metric.value);
    if (!metric.op || metric.op === 'count') return String(rows.length);
    if (metric.op === 'count_where' && metric.field) {
      return String(rows.filter((row) => row[metric.field!] === metric.equals).length);
    }
    return '—';
  }

  type ChartEntry = { label: string; value: number };
  type ChartType = 'bar' | 'line' | 'pie' | 'scatter';

  function chartType(view: GeneratedUiView): ChartType {
    const type = String(view.chart_type || view.chart || 'bar').trim();
    return ['bar', 'line', 'pie', 'scatter'].includes(type) ? type as ChartType : 'bar';
  }

  function chartData(view: GeneratedUiView, rows: Record<string, any>[]): ChartEntry[] {
    const configuredRows = staticRowsForView(view);
    if (configuredRows.length && 'value' in configuredRows[0]) {
      return configuredRows.map((row) => ({
        label: String(row.label ?? row.name ?? row.title ?? 'Item'),
        value: Number(row.value ?? 0),
      }));
    }
    const groupKey = view.group_by || columnsForView(view, rows)[0]?.key;
    const counts = new Map<string, number>();
    rows.forEach((row) => {
      const label = formatValue(groupKey ? row[groupKey] : 'Records');
      counts.set(label, (counts.get(label) || 0) + 1);
    });
    return Array.from(counts.entries()).map(([label, value]) => ({ label, value }));
  }

  function chartMax(data: ChartEntry[]): number {
    return Math.max(...data.map((entry) => Number(entry.value) || 0), 1);
  }

  function lineChartPoints(data: ChartEntry[]) {
    const max = chartMax(data);
    const usableWidth = 300;
    const step = data.length > 1 ? usableWidth / (data.length - 1) : 0;
    return data.map((entry, index) => {
      const x = 36 + index * step;
      const y = 150 - ((Number(entry.value) || 0) / max) * 118;
      return `${x},${y}`;
    }).join(' ');
  }

  function scatterPoint(entry: ChartEntry, index: number, data: ChartEntry[]) {
    const max = chartMax(data);
    const step = data.length > 1 ? 300 / (data.length - 1) : 0;
    return {
      x: 36 + index * step,
      y: 150 - ((Number(entry.value) || 0) / max) * 118,
    };
  }

  function pieSlices(data: ChartEntry[]) {
    const total = data.reduce((sum, entry) => sum + Math.max(Number(entry.value) || 0, 0), 0) || 1;
    const colors = ['#57cfa0', '#8db7ff', '#4BACB8', '#d86f78', '#a98dff', '#64c6d9'];
    let cursor = -Math.PI / 2;
    return data.map((entry, index) => {
      const value = Math.max(Number(entry.value) || 0, 0);
      const angle = (value / total) * Math.PI * 2;
      const start = cursor;
      cursor += angle;
      const end = cursor;
      const largeArc = angle > Math.PI ? 1 : 0;
      const cx = 92;
      const cy = 92;
      const radius = 72;
      const x1 = cx + radius * Math.cos(start);
      const y1 = cy + radius * Math.sin(start);
      const x2 = cx + radius * Math.cos(end);
      const y2 = cy + radius * Math.sin(end);
      return {
        path: `M ${cx} ${cy} L ${x1} ${y1} A ${radius} ${radius} 0 ${largeArc} 1 ${x2} ${y2} Z`,
        color: colors[index % colors.length],
        label: entry.label,
        value,
      };
    });
  }
</script>

<section class="generated-ui generated-app-shell" class:is-dock={surface === 'dock'} class:has-board={hasBoardView}>
  <header class="generated-ui__header generated-app-shell__header">
    <div class="generated-ui__title-block">
      <span class="generated-ui__eyebrow">Generated UI</span>
      <h2>{spec?.title || app.name}</h2>
      {#if spec?.description || app.description}
        <p>{spec?.description || app.description}</p>
      {/if}
    </div>

    <div class="generated-ui__actions">
      {#each structuredActions as action (action.key)}
        <button
          type="button"
          class="generated-ui__action-button"
          title={action.description || action.label}
          disabled={busyAction[action.key] || action.disabled}
          onclick={() => runGeneratedAction(action)}
        >
          {busyAction[action.key] ? 'Running' : action.label}
        </button>
      {/each}
      <label class="generated-ui__search">
        <span>Search</span>
        <input
          value={search}
          placeholder="Filter records"
          oninput={(event) => setSearch((event.target as HTMLInputElement).value)}
        />
      </label>
      <button type="button" class="generated-ui__icon-button" title="Refresh records" onclick={loadBoundRecords}>
        <ConstellationIcon name="refresh" size={14} stroke={1.9} />
      </button>
      {#if onclose}
        <button type="button" class="generated-ui__icon-button" title="Close app" onclick={onclose}>
          <ConstellationIcon name="close" size={14} stroke={1.9} />
        </button>
      {/if}
    </div>
  </header>

  {#if parsedSpec.error}
    <div class="generated-ui__empty">{parsedSpec.error}</div>
  {:else}
    {#if loadError}
      <div class="generated-ui__notice">{loadError}</div>
    {/if}
    {#if actionError}
      <div class="generated-ui__notice generated-ui__notice--error">{actionError}</div>
    {:else if actionNotice}
      <div class="generated-ui__notice">{actionNotice}</div>
    {/if}

    <div class="generated-ui__body" aria-busy={loadingRecords}>
      {#each views as view, index (viewId(view, index))}
        {@const rows = filterRows(rowsForView(view))}
        {@const columns = columnsForView(view, rows)}
        <section class={`generated-ui__view generated-ui__view--${view.type || 'table'}`}>
          <div class="generated-ui__view-heading">
            <div>
              <h3>{view.title || labelFromKey(view.type || 'records')}</h3>
              {#if view.description}
                <p>{view.description}</p>
              {/if}
            </div>
            <span>{rows.length} shown</span>
          </div>

          {#if view.type === 'metrics'}
            <div class="generated-ui__metrics">
              {#each (view.metrics || [{ label: 'Records', op: 'count' }]) as metric}
                <div class="generated-ui__metric">
                  <span>{metric.label || labelFromKey(metric.key || metric.op || 'metric')}</span>
                  <strong>{metricValue(metric, rows)}</strong>
                </div>
              {/each}
            </div>
          {:else if view.type === 'board'}
            {@const groupKey = boardGroupKey(view)}
            {@const groups = boardGroups(view, rows)}
            {@const badgeKeys = boardCardBadgeKeys(view, columns)}
            <div class="generated-ui__board">
              {#each groups as group}
                {@const groupRows = rowsForBoardGroup(rows, groupKey, group.value)}
                <section
                  class="generated-ui__board-column"
                  role="list"
                  aria-label={`${group.label} ${labelFromKey(groupKey)}`}
                  ondragover={(event) => event.preventDefault()}
                  ondrop={(event) => dropBoardCard(event, view, index, rows, group.value)}
                >
                  <header>
                    <strong>{group.label}</strong>
                    <span>{groupRows.length}</span>
                  </header>
                  <div class="generated-ui__board-cards">
                    {#each groupRows as row}
                      <article
                        class="generated-ui__board-card"
                        role="listitem"
                        draggable={canMoveBoardCard(view, groupKey, row)}
                        ondragstart={(event) => startBoardDrag(event, view, index, row, groupKey)}
                      >
                        <strong>{boardCardTitle(view, row, columns)}</strong>
                        {#if boardCardSubtitle(view, row)}
                          <p>{boardCardSubtitle(view, row)}</p>
                        {/if}
                        {#if badgeKeys.length}
                          <div>
                            {#each badgeKeys as key}
                              {#if row[key] !== undefined && row[key] !== null && row[key] !== ''}
                                <span>{formatValue(row[key])}</span>
                              {/if}
                            {/each}
                          </div>
                        {/if}
                      </article>
                    {:else}
                      <div class="generated-ui__board-empty">{view.empty_state || 'No records.'}</div>
                    {/each}
                  </div>
                </section>
              {/each}
            </div>
          {:else if view.type === 'chart'}
            {@const data = chartData(view, rows)}
            {@const type = chartType(view)}
            {@const max = chartMax(data)}
            {#if data.length === 0}
              <div class="generated-ui__empty">{view.empty_state || 'No chart data yet.'}</div>
            {:else if type === 'line'}
              <div class="generated-ui__chart generated-ui__chart--svg" data-chart-type={type}>
                <svg viewBox="0 0 372 170" role="img" aria-label={view.title || 'Line chart'}>
                  <line x1="36" y1="150" x2="350" y2="150"></line>
                  <polyline points={lineChartPoints(data)}></polyline>
                  {#each data as entry, entryIndex}
                    {@const point = scatterPoint(entry, entryIndex, data)}
                    <circle cx={point.x} cy={point.y} r="4"></circle>
                  {/each}
                </svg>
              </div>
            {:else if type === 'pie'}
              <div class="generated-ui__chart generated-ui__chart--pie" data-chart-type={type}>
                <svg viewBox="0 0 184 184" role="img" aria-label={view.title || 'Pie chart'}>
                  {#each pieSlices(data) as slice}
                    <path d={slice.path} fill={slice.color}></path>
                  {/each}
                </svg>
                <div class="generated-ui__chart-legend">
                  {#each pieSlices(data) as slice}
                    <span><i style:background={slice.color}></i>{slice.label}: {slice.value}</span>
                  {/each}
                </div>
              </div>
            {:else if type === 'scatter'}
              <div class="generated-ui__chart generated-ui__chart--svg" data-chart-type={type}>
                <svg viewBox="0 0 372 170" role="img" aria-label={view.title || 'Scatter chart'}>
                  <line x1="36" y1="150" x2="350" y2="150"></line>
                  {#each data as entry, entryIndex}
                    {@const point = scatterPoint(entry, entryIndex, data)}
                    <circle cx={point.x} cy={point.y} r="5"></circle>
                  {/each}
                </svg>
              </div>
            {:else}
              <div class="generated-ui__chart" data-chart-type={type}>
                {#each data as entry}
                  <div class="generated-ui__bar-row">
                    <span>{entry.label}</span>
                    <div class="generated-ui__bar-track">
                      <i style={`width:${Math.max(4, (entry.value / max) * 100)}%`}></i>
                    </div>
                    <strong>{entry.value}</strong>
                  </div>
                {/each}
              </div>
            {/if}
          {:else if view.type === 'list' || view.type === 'cards'}
            <div class="generated-ui__rows">
              {#each rows as row, rowIndex}
                <article class="generated-ui__row">
                  <strong>{formatValue(row.title || row.name || row[columns[0]?.key])}</strong>
                  <div>
                    {#each columns.slice(0, 4) as column}
                      <span>{column.label || labelFromKey(column.key)}: {formatValue(cellValue(row, column), column)}</span>
                    {/each}
                  </div>
                </article>
              {:else}
                <div class="generated-ui__empty">{view.empty_state || 'No records yet.'}</div>
              {/each}
            </div>
          {:else if view.type === 'detail'}
            {#if rows[0]}
              <dl class="generated-ui__detail">
                {#each columns as column}
                  <div>
                    <dt>{column.label || labelFromKey(column.key)}</dt>
                    <dd>{formatValue(cellValue(rows[0], column), column)}</dd>
                  </div>
                {/each}
              </dl>
            {:else}
              <div class="generated-ui__empty">{view.empty_state || 'No record selected.'}</div>
            {/if}
          {:else}
            <div class="generated-ui__table-wrap">
              <table>
                <thead>
                  <tr>
                    {#each columns as column}
                      <th style:width={column.width}>{column.label || labelFromKey(column.key)}</th>
                    {/each}
                  </tr>
                </thead>
                <tbody>
                  {#each rows as row, rowIndex}
                    <tr>
                      {#each columns as column}
                        <td>
                          {#if canEditColumn(view, column)}
                            {@const record = row.__record as DomainRecordRead | undefined}
                            {@const cacheKey = `${bindingAliasForView(view)}:${record?.id}:${column.key}`}
                            <select
                              value={String(cellValue(row, column) ?? '')}
                              disabled={busyCell[cacheKey]}
                              onchange={(event) => updateCell(view, row, column, (event.target as HTMLSelectElement).value)}
                            >
                              {#each editableOptions(column) as option}
                                <option value={String(optionValue(option))}>{optionLabel(option)}</option>
                              {/each}
                            </select>
                          {:else}
                            {formatValue(cellValue(row, column), column)}
                          {/if}
                        </td>
                      {/each}
                    </tr>
                  {:else}
                    <tr>
                      <td colspan={Math.max(columns.length, 1)}>{view.empty_state || 'No records yet.'}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}

          {#if shouldShowCreate(view)}
            {@const fields = createFieldsForView(view, rows)}
            <form class="generated-ui__create" onsubmit={(event) => { event.preventDefault(); void createRecord(view, index); }}>
              {#each fields as field}
                <label>
                  <span>{field.label || labelFromKey(field.key)}</span>
                  <input
                    value={draftByView[viewId(view, index)]?.[field.key] ?? ''}
                    oninput={(event) => setDraftValue(view, index, field.key, (event.target as HTMLInputElement).value)}
                  />
                </label>
              {/each}
              <button type="submit" disabled={busyCreate[viewId(view, index)]}>
                <ConstellationIcon name="plus" size={13} stroke={2} />
                Add
              </button>
            </form>
          {:else if wantsCreate(view)}
            <div class="generated-ui__create-note">
              Create controls need a Domain binding with create access.
            </div>
          {/if}
        </section>
      {/each}
    </div>
  {/if}
</section>

<style>
.generated-ui {
  width: min(760px, calc(100vw - 28px));
  max-height: min(820px, calc(100vh - 112px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 18px;
}

.generated-ui.has-board {
  width: min(1040px, calc(100vw - 28px));
}

.generated-ui.is-dock {
  width: 100%;
  max-height: none;
  min-height: 100%;
  border-radius: 0;
}

.generated-ui__header {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  padding: 18px 18px 14px;
}

.generated-ui__title-block {
  display: grid;
  gap: 5px;
  min-width: 0;
}

.generated-ui__eyebrow {
  color: var(--constellation-color-spectral);
  font-family: var(--constellation-font-mono, monospace);
  font-size: 10px;
  font-weight: 680;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.generated-ui h2,
.generated-ui h3,
.generated-ui p {
  margin: 0;
  letter-spacing: 0;
}

.generated-ui h2 {
  overflow: hidden;
  color: var(--constellation-section-title);
  font-size: 19px;
  font-weight: 680;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.generated-ui p {
  color: var(--constellation-section-description);
  font-size: 12px;
  line-height: 1.45;
}

.generated-ui__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.generated-ui__search {
  display: grid;
  gap: 4px;
  width: min(220px, 28vw);
}

.generated-ui__search span,
.generated-ui__create span,
.generated-ui__detail dt {
  color: var(--constellation-label-meta);
  font-family: var(--constellation-font-mono, monospace);
  font-size: 9px;
  font-weight: 680;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.generated-ui input,
.generated-ui select {
  min-height: 34px;
  border: 1px solid var(--constellation-control-field-border);
  border-radius: 10px;
  background: var(--constellation-control-field-background);
  color: var(--constellation-color-text-primary);
  font: inherit;
}

.generated-ui input {
  min-width: 0;
  padding: 0 10px;
}

.generated-ui select {
  width: 100%;
  padding: 0 8px;
}

.generated-ui__icon-button,
.generated-ui__action-button,
.generated-ui__create button {
  display: inline-flex;
  min-height: 34px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid var(--constellation-control-button-secondary-border);
  border-radius: 10px;
  background: var(--constellation-control-button-secondary-background);
  color: var(--constellation-control-button-secondary-text);
  cursor: pointer;
}

.generated-ui__action-button {
  max-width: 180px;
  padding: 0 12px;
  overflow: hidden;
  font-size: 12px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.generated-ui__action-button:disabled,
.generated-ui__icon-button:disabled,
.generated-ui__create button:disabled {
  cursor: default;
  opacity: 0.62;
}

.generated-ui__icon-button {
  width: 34px;
  padding: 0;
}

.generated-ui__body {
  min-height: 0;
  overflow: auto;
  scrollbar-color: color-mix(in srgb, var(--constellation-color-spectral) 28%, transparent) transparent;
}

.generated-ui__view {
  display: grid;
  gap: 12px;
  padding: 16px 18px 18px;
  border-bottom: 1px solid var(--constellation-surface-panel-separator);
}

.generated-ui__view:last-child {
  border-bottom: 0;
}

.generated-ui__view-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.generated-ui__view-heading h3 {
  color: var(--constellation-section-title);
  font-size: 14px;
  font-weight: 650;
}

.generated-ui__view-heading > span {
  flex: 0 0 auto;
  color: var(--constellation-label-meta);
  font-family: var(--constellation-font-mono, monospace);
  font-size: 10px;
}

.generated-ui__notice,
.generated-ui__empty {
  margin: 14px 18px;
  padding: 13px;
  border: 1px dashed var(--constellation-control-pill-info-border);
  border-radius: 12px;
  background: var(--constellation-surface-nested-background);
  color: var(--constellation-section-description);
  font-size: 12px;
}

.generated-ui__table-wrap {
  overflow: auto;
}

.generated-ui table {
  width: 100%;
  min-width: 460px;
  border-collapse: collapse;
  font-size: 12px;
}

.generated-ui th {
  padding: 0 10px 9px;
  color: var(--constellation-label-meta);
  font-family: var(--constellation-font-mono, monospace);
  font-size: 9px;
  font-weight: 680;
  letter-spacing: 0.12em;
  text-align: left;
  text-transform: uppercase;
}

.generated-ui td {
  max-width: 220px;
  padding: 10px;
  border-top: 1px solid var(--constellation-surface-panel-separator);
  color: var(--constellation-color-text-secondary);
  vertical-align: middle;
}

.generated-ui__rows {
  display: grid;
  gap: 2px;
}

.generated-ui__board {
  display: grid;
  grid-auto-columns: minmax(210px, 1fr);
  grid-auto-flow: column;
  gap: 10px;
  overflow-x: auto;
  padding-bottom: 4px;
}

.generated-ui__board-column {
  min-width: 0;
  display: grid;
  grid-template-rows: auto minmax(92px, 1fr);
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--constellation-surface-panel-separator);
  border-radius: 12px;
  background: var(--constellation-surface-nested-background);
}

.generated-ui__board-column header {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.generated-ui__board-column header strong {
  overflow: hidden;
  color: var(--constellation-section-title);
  font-size: 12px;
  font-weight: 680;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.generated-ui__board-column header span {
  flex: 0 0 auto;
  color: var(--constellation-label-meta);
  font-family: var(--constellation-font-mono, monospace);
  font-size: 10px;
}

.generated-ui__board-cards {
  display: grid;
  align-content: start;
  gap: 8px;
  min-height: 80px;
}

.generated-ui__board-card {
  display: grid;
  gap: 7px;
  min-width: 0;
  padding: 10px;
  border: 1px solid var(--constellation-surface-panel-separator);
  border-radius: 10px;
  background: var(--constellation-surface-panel-background);
}

.generated-ui__board-card[draggable='true'] {
  cursor: grab;
}

.generated-ui__board-card strong,
.generated-ui__board-card p {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.generated-ui__board-card strong {
  color: var(--constellation-section-title);
  font-size: 12px;
  line-height: 1.35;
}

.generated-ui__board-card p {
  white-space: nowrap;
}

.generated-ui__board-card div {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  min-width: 0;
}

.generated-ui__board-card div span {
  max-width: 100%;
  overflow: hidden;
  border: 1px solid var(--constellation-control-pill-info-border);
  border-radius: 999px;
  color: var(--constellation-color-text-tertiary);
  font-size: 10px;
  padding: 3px 6px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.generated-ui__board-empty {
  display: grid;
  min-height: 56px;
  place-items: center;
  border: 1px dashed var(--constellation-surface-panel-separator);
  border-radius: 10px;
  color: var(--constellation-color-text-muted);
  font-size: 11px;
}

.generated-ui__row {
  display: grid;
  gap: 7px;
  padding: 12px 0;
  border-top: 1px solid var(--constellation-surface-panel-separator);
}

.generated-ui__row strong {
  color: var(--constellation-section-title);
  font-size: 13px;
}

.generated-ui__row div {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  color: var(--constellation-color-text-tertiary);
  font-size: 11px;
}

.generated-ui__metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
  gap: 10px;
}

.generated-ui__metric {
  display: grid;
  gap: 6px;
  padding: 12px 0;
  border-top: 1px solid var(--constellation-surface-panel-separator);
}

.generated-ui__metric span {
  color: var(--constellation-color-text-muted);
  font-size: 11px;
}

.generated-ui__metric strong {
  color: var(--constellation-section-title);
  font-size: 22px;
  font-weight: 680;
}

.generated-ui__chart {
  display: grid;
  gap: 9px;
}

.generated-ui__chart--svg,
.generated-ui__chart--pie {
  align-items: center;
}

.generated-ui__chart--svg svg,
.generated-ui__chart--pie svg {
  width: 100%;
  min-height: 180px;
}

.generated-ui__chart--svg line {
  stroke: var(--constellation-surface-panel-separator);
  stroke-width: 1;
}

.generated-ui__chart--svg polyline {
  fill: none;
  stroke: var(--constellation-color-spectral);
  stroke-linejoin: round;
  stroke-width: 3;
}

.generated-ui__chart--svg circle {
  fill: var(--positive);
  stroke: var(--constellation-color-badge-ring);
  stroke-width: 2;
}

.generated-ui__chart--pie {
  grid-template-columns: minmax(150px, 190px) minmax(0, 1fr);
  gap: 14px;
}

.generated-ui__chart-legend {
  display: grid;
  gap: 7px;
  color: var(--constellation-color-text-secondary);
  font-size: 11px;
}

.generated-ui__chart-legend span {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 7px;
}

.generated-ui__chart-legend i {
  width: 9px;
  height: 9px;
  flex: 0 0 auto;
  border-radius: 999px;
}

.generated-ui__bar-row {
  display: grid;
  grid-template-columns: minmax(72px, 0.8fr) minmax(120px, 2fr) 42px;
  gap: 10px;
  align-items: center;
  color: var(--constellation-color-text-secondary);
  font-size: 12px;
}

.generated-ui__bar-track {
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--constellation-control-slider-track);
}

.generated-ui__bar-track i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--positive), var(--constellation-color-spectral));
}

.generated-ui__detail {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
  gap: 13px 18px;
  margin: 0;
}

.generated-ui__detail div {
  display: grid;
  gap: 5px;
}

.generated-ui__detail dd {
  margin: 0;
  color: var(--constellation-color-text-secondary);
  font-size: 13px;
}

.generated-ui__create {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)) auto;
  gap: 10px;
  align-items: end;
  padding-top: 4px;
}

.generated-ui__create label {
  display: grid;
  gap: 5px;
}

.generated-ui__create button {
  padding: 0 12px;
}

.generated-ui__create-note {
  color: var(--constellation-color-text-muted);
  font-size: 11px;
}

@media (max-width: 680px) {
  .generated-ui {
    width: calc(100vw - 20px);
    max-height: calc(100vh - 96px);
  }

  .generated-ui__header {
    grid-template-columns: 1fr;
  }

  .generated-ui__actions {
    align-items: end;
  }

  .generated-ui__search {
    width: 100%;
  }

  .generated-ui__create {
    grid-template-columns: 1fr;
  }

  .generated-ui__chart--pie {
    grid-template-columns: 1fr;
  }
}

</style>
