<script lang="ts">
  import { getContext, onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationEmptyState,
    ConstellationIcon,
    ConstellationNotice,
    ConstellationPageFrame,
    ConstellationPanel,
    ConstellationPill,
    ConstellationSearchField,
    ConstellationSectionHeader,
    ConstellationSkeletonBlock,
  } from '$lib/components/constellation';
  import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';
  import {
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
    type ConstellationPageFrameModalContext,
  } from '$lib/components/constellation/constellationPageFrameContext';
  import { ui } from '$lib/stores/ui.svelte';

  type EditorMode = 'idle' | 'create';
  type PillTone = 'muted' | 'warning' | 'success' | 'danger' | 'info';

  interface SkillTrigger {
    direction?: string;
    pattern?: string;
  }

  interface SkillGuardrail {
    text?: string;
    severity?: string;
  }

  interface SkillAsset {
    id?: number | null;
    path: string;
    asset_kind?: string | null;
    mime_type?: string | null;
    size_bytes?: number | null;
    has_inline_content?: boolean;
  }

  interface SkillPackage {
    package_kind?: string;
    is_bundle_backed?: boolean;
    namespace?: string | null;
    package_name?: string | null;
    display_name?: string | null;
    description?: string | null;
    source_kind?: string | null;
    trust_level?: string | null;
    visibility?: string | null;
    semver?: string | null;
    effective_digest?: string | null;
    bundle_digest?: string | null;
    installation_id?: number | null;
    enabled?: boolean | null;
    enabled_scope?: string | null;
    pinned?: boolean | null;
    update_policy?: string | null;
    review_status?: string | null;
    asset_count?: number | null;
    asset_counts?: Record<string, number>;
    assets?: SkillAsset[];
    permissions?: Record<string, unknown>;
  }

  interface Skill {
    id: number;
    name: string;
    description?: string | null;
    procedure?: string | null;
    version?: number | null;
    maturity?: string | null;
    use_count?: number | null;
    partial_count?: number | null;
    avg_duration_sec?: number | null;
    last_used?: string | null;
    pitfalls?: string[] | null;
    refinements?: string[] | null;
    triggers?: SkillTrigger[] | null;
    guardrails?: SkillGuardrail[] | null;
    thinking_tier?: string | null;
    source_kind?: string | null;
    trust_level?: string | null;
    skill_installation_id?: number | null;
    bundle_version_id?: number | null;
    bundle_digest?: string | null;
    effective_digest?: string | null;
    overlay_revision?: number | null;
  }

  interface EnhancedSkill {
    skill: Skill;
    package: SkillPackage;
    needs_attention?: boolean;
    convert_to_bundle_available?: boolean;
  }

  interface AssetPreview extends SkillAsset {
    content?: string | null;
    truncated?: boolean;
  }

  interface AssetDraft {
    path: string;
    asset_kind: string;
    mime_type: string;
    content: string;
  }

  interface AssetRoot {
    root: string;
    kind: string;
    label: string;
    sample: string;
    icon: ConstellationIconName;
  }

  interface AssetGroup extends AssetRoot {
    assets: SkillAsset[];
    count: number;
  }

  interface SkillForm {
    name: string;
    description: string;
    procedure: string;
    thinking_tier: string;
    triggers: Required<SkillTrigger>[];
    guardrails: Required<SkillGuardrail>[];
    pitfalls: string[];
    refinements: string[];
    create_as_package: boolean;
    assets: AssetDraft[];
  }

  interface AttentionReason {
    label: string;
    detail: string;
    tone: PillTone;
  }

  const TRIGGER_DIRECTIONS = ['for', 'not_for'];
  const GUARDRAIL_SEVERITIES = ['info', 'warning', 'critical'];
  const ASSET_ROOTS: AssetRoot[] = [
    { root: 'references', kind: 'reference', label: 'Reference', sample: 'references/context.md', icon: 'document' },
    { root: 'scripts', kind: 'script', label: 'Script', sample: 'scripts/verify.py', icon: 'code' },
    { root: 'examples', kind: 'example', label: 'Example', sample: 'examples/happy.md', icon: 'skills' },
    { root: 'templates', kind: 'template', label: 'Template', sample: 'templates/prompt.md', icon: 'tool' },
    { root: 'schemas', kind: 'schema', label: 'Schema', sample: 'schemas/input.schema.json', icon: 'database' },
    { root: 'evals', kind: 'eval', label: 'Eval', sample: 'evals/golden.jsonl', icon: 'test' },
  ];
  const TEXT_ASSET_ACCEPT = [
    '.css',
    '.csv',
    '.html',
    '.ini',
    '.js',
    '.json',
    '.jsonl',
    '.md',
    '.mjs',
    '.py',
    '.rb',
    '.rst',
    '.sh',
    '.sql',
    '.toml',
    '.ts',
    '.tsv',
    '.txt',
    '.yaml',
    '.yml',
  ].join(',');

  let items = $state<EnhancedSkill[]>([]);
  let loading = $state(true);
  let loadError = $state('');
  let selectedId = $state<number | null>(null);
  let search = $state('');
  let editorMode = $state<EditorMode>('idle');
  let saving = $state(false);
  let deleting = $state(false);
  let converting = $state(false);
  let inlineDraftSkillId = $state<number | null>(null);
  let inlineDraft = $state<SkillForm | null>(null);
  let inlineSaving = $state(false);
  let assetLoading = $state(false);
  let assetSaving = $state(false);
  let assetDeleting = $state(false);

  const workspacePageModalContext = getContext<ConstellationPageFrameModalContext | undefined>(
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
  );

  $effect(() => {
    return workspacePageModalContext?.registerRefreshAction({
      label: 'Refresh skills',
      onclick: () => loadSkills(),
    });
  });
  let assetPreview = $state<AssetPreview | null>(null);
  let assetEditorOpen = $state(false);
  let editingAssetPath = $state<string | null>(null);
  let assetForm = $state<AssetDraft>(emptyAssetDraft());
  let form = $state<SkillForm>(emptyForm());

  const selectedItem = $derived.by(
    () => items.find((item) => item.skill.id === selectedId) ?? null,
  );
  const selectedSkill = $derived(selectedItem?.skill ?? null);
  const selectedPackage = $derived(selectedItem?.package ?? null);
  const selectedPackageFiles = $derived(
    array(selectedPackage?.assets).filter((asset) => asset.path !== 'SKILL.md'),
  );
  const selectedAssetGroups = $derived.by<AssetGroup[]>(() =>
    ASSET_ROOTS.map((root) => {
      const assets = selectedPackageFiles.filter((asset) => assetMatchesRoot(asset, root));
      return { ...root, assets, count: assets.length };
    }),
  );
  const filteredItems = $derived.by(() => {
    const needle = search.trim().toLowerCase();
    return items
      .filter((item) => {
        const skill = item.skill;
        if (!needle) return true;
        return [
          skill.name,
          skill.description,
          skill.procedure,
          ...textItems(skill.pitfalls),
          ...textItems(skill.refinements),
          ...array(skill.triggers).map((trigger) => trigger.pattern ?? ''),
          ...array(skill.guardrails).map((guardrail) => guardrail.text ?? ''),
          ...array(item.package?.assets).map((asset) => asset.path),
        ]
          .join(' ')
          .toLowerCase()
          .includes(needle);
      })
      .sort((a, b) => numberValue(b.skill.use_count) - numberValue(a.skill.use_count));
  });
  const inlineDraftDirty = $derived.by(() => {
    if (!selectedSkill || !inlineDraft || inlineDraftSkillId !== selectedSkill.id) return false;
    return JSON.stringify(payloadFromDraft(inlineDraft)) !== JSON.stringify(payloadFromDraft(formFromSkill(selectedSkill)));
  });

  $effect(() => {
    if (!selectedSkill) {
      inlineDraftSkillId = null;
      inlineDraft = null;
      return;
    }

    if (inlineDraftSkillId !== selectedSkill.id) {
      inlineDraftSkillId = selectedSkill.id;
      inlineDraft = formFromSkill(selectedSkill);
    }
  });

  onMount(() => {
    void loadSkills();
  });

  function emptyForm(): SkillForm {
    return {
      name: '',
      description: '',
      procedure: '',
      thinking_tier: 'medium',
      triggers: [],
      guardrails: [],
      pitfalls: [],
      refinements: [],
      create_as_package: true,
      assets: [],
    };
  }

  function emptyAssetDraft(root = 'references'): AssetDraft {
    const option = ASSET_ROOTS.find((item) => item.root === root) ?? ASSET_ROOTS[0];
    return {
      path: option.sample,
      asset_kind: option.kind,
      mime_type: '',
      content: '',
    };
  }

  function array<T>(value: T[] | null | undefined): T[] {
    return Array.isArray(value) ? value : [];
  }

  function stringList(value: string[] | null | undefined): string[] {
    return Array.isArray(value) ? value.filter((item) => typeof item === 'string') : [];
  }

  function itemText(value: unknown): string {
    if (typeof value === 'string') return value.trim();
    if (!value || typeof value !== 'object') return '';
    const record = value as Record<string, unknown>;
    for (const key of ['text', 'change', 'title', 'summary', 'detail', 'description', 'reason']) {
      const candidate = record[key];
      if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
    }
    return '';
  }

  function textItems(value: unknown[] | null | undefined): string[] {
    return Array.isArray(value) ? value.map(itemText).filter(Boolean) : [];
  }

  function numberValue(value: number | null | undefined): number {
    return typeof value === 'number' && Number.isFinite(value) ? value : 0;
  }

  function assetMatchesRoot(asset: SkillAsset, root: AssetRoot): boolean {
    return asset.asset_kind === root.kind || asset.path.startsWith(`${root.root}/`);
  }

  function assetKindFromPath(path: string): string {
    const root = path.trim().split('/', 1)[0];
    return ASSET_ROOTS.find((item) => item.root === root)?.kind ?? 'reference';
  }

  function assetSampleForKind(kind: string): string {
    return ASSET_ROOTS.find((item) => item.kind === kind)?.sample ?? 'references/context.md';
  }

  function assetRootForKind(kind: string): string {
    return ASSET_ROOTS.find((item) => item.kind === kind)?.root ?? 'references';
  }

  function triggerDirectionLabel(direction: string | null | undefined): string {
    return direction === 'not_for' ? 'Do not use for' : 'Use for';
  }

  function safeAssetFileName(name: string): string {
    const cleaned = name
      .trim()
      .replace(/[\\/]/g, '-')
      .replace(/\s+/g, '-')
      .replace(/[^A-Za-z0-9._-]/g, '-');
    return cleaned || 'asset.txt';
  }

  function pathForImportedFile(file: File, draft: AssetDraft, keepExisting: boolean): string {
    const currentPath = draft.path.trim();
    const samplePath = assetSampleForKind(draft.asset_kind);
    if (keepExisting && currentPath) return currentPath;
    if (currentPath && currentPath !== samplePath) return currentPath;
    return `${assetRootForKind(draft.asset_kind)}/${safeAssetFileName(file.name)}`;
  }

  function syncAssetKindFromPath(draft: AssetDraft): AssetDraft {
    return {
      ...draft,
      asset_kind: assetKindFromPath(draft.path),
    };
  }

  function errorMessage(error: unknown, fallback: string): string {
    if (error instanceof Error) return error.message;
    if (error && typeof error === 'object' && 'detail' in error) {
      return String((error as { detail: unknown }).detail);
    }
    return fallback;
  }

  function plural(count: number, singular: string, pluralLabel = `${singular}s`): string {
    return `${count} ${count === 1 ? singular : pluralLabel}`;
  }

  function formatBytes(value: number | null | undefined): string {
    const bytes = numberValue(value);
    if (!bytes) return '0 B';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function attentionReasons(item: EnhancedSkill): AttentionReason[] {
    const skill = item.skill;
    const useCount = numberValue(skill.use_count);
    const reasons: AttentionReason[] = [];
    if (!skill.procedure?.trim()) {
      reasons.push({ label: 'Needs instructions', detail: 'Add the steps Illo should follow.', tone: 'danger' });
    }
    if (!array(skill.triggers).length) {
      reasons.push({ label: 'Add examples', detail: 'Add a few requests that should use this workflow.', tone: 'warning' });
    }
    if (useCount >= 5 && !array(skill.guardrails).length) {
      reasons.push({ label: 'Add guardrails', detail: 'Add checks before this workflow is reused.', tone: 'warning' });
    }
    return reasons;
  }

  function advisoryItems(skill: Skill): Array<{ label: string; detail: string; tone: PillTone }> {
    const items: Array<{ label: string; detail: string; tone: PillTone }> = [];
    if (!skill.procedure?.trim()) {
      items.push({ label: 'Needs instructions', detail: 'Add the steps Illo should follow.', tone: 'danger' });
    }
    if (!array(skill.guardrails).length) {
      items.push({ label: 'Add guardrails', detail: 'Useful checks before Illo runs this workflow.', tone: 'muted' });
    }
    if (!array(skill.triggers).length) {
      items.push({ label: 'Add examples', detail: 'A few phrases help Illo know when to use it.', tone: 'muted' });
    }
    return items;
  }

  function formFromSkill(skill: Skill): SkillForm {
    return {
      name: skill.name ?? '',
      description: skill.description ?? '',
      procedure: skill.procedure ?? '',
      thinking_tier: skill.thinking_tier ?? 'medium',
      triggers: array(skill.triggers).map((trigger) => ({
        direction: trigger.direction || 'for',
        pattern: trigger.pattern || '',
      })),
      guardrails: array(skill.guardrails).map((guardrail) => ({
        severity: guardrail.severity || 'warning',
        text: guardrail.text || '',
      })),
      pitfalls: stringList(skill.pitfalls),
      refinements: stringList(skill.refinements),
      create_as_package: false,
      assets: [],
    };
  }

  function payloadFromDraft(draft: SkillForm) {
    return {
      name: draft.name.trim(),
      description: draft.description.trim(),
      procedure: draft.procedure.trim(),
      thinking_tier: draft.thinking_tier,
      triggers: draft.triggers
        .filter((trigger) => trigger.pattern.trim())
        .map((trigger) => ({
          direction: trigger.direction || 'for',
          pattern: trigger.pattern.trim(),
        })),
      guardrails: draft.guardrails
        .filter((guardrail) => guardrail.text.trim())
        .map((guardrail) => ({
          severity: guardrail.severity || 'warning',
          text: guardrail.text.trim(),
        })),
      pitfalls: draft.pitfalls.map((item) => item.trim()).filter(Boolean),
      refinements: draft.refinements.map((item) => item.trim()).filter(Boolean),
    };
  }

  function payloadFromForm() {
    return payloadFromDraft(form);
  }

  async function loadSkills(preselectId: number | null = selectedId) {
    loading = true;
    loadError = '';
    try {
      const nextItems = await api.listEnhancedSkills();
      items = Array.isArray(nextItems) ? nextItems : [];
      if (preselectId && items.some((item) => item.skill.id === preselectId)) {
        selectedId = preselectId;
      } else {
        selectedId = null;
      }
      assetPreview = null;
    } catch (error) {
      loadError = errorMessage(error, 'Unable to load skills.');
      ui.toast(loadError, 'error');
    } finally {
      loading = false;
    }
  }

  function selectItem(item: EnhancedSkill) {
    selectedId = selectedId === item.skill.id ? null : item.skill.id;
    assetPreview = null;
    assetEditorOpen = false;
    editingAssetPath = null;
  }

  function startCreate() {
    editorMode = 'create';
    form = emptyForm();
    assetPreview = null;
    assetEditorOpen = false;
    editingAssetPath = null;
  }

  function cancelEdit() {
    editorMode = 'idle';
    form = emptyForm();
  }

  function resetInlineDraft() {
    if (!selectedSkill) return;
    inlineDraftSkillId = selectedSkill.id;
    inlineDraft = formFromSkill(selectedSkill);
  }

  function addInlineTrigger() {
    if (!inlineDraft) return;
    inlineDraft.triggers = [...inlineDraft.triggers, { direction: 'for', pattern: '' }];
  }

  function addInlineGuardrail() {
    if (!inlineDraft) return;
    inlineDraft.guardrails = [...inlineDraft.guardrails, { severity: 'warning', text: '' }];
  }

  function addInlinePitfall() {
    if (!inlineDraft) return;
    inlineDraft.pitfalls = [...inlineDraft.pitfalls, ''];
  }

  function addInlineRefinement() {
    if (!inlineDraft) return;
    inlineDraft.refinements = [...inlineDraft.refinements, ''];
  }

  function removeInlineTrigger(index: number) {
    if (!inlineDraft) return;
    inlineDraft.triggers = inlineDraft.triggers.filter((_, i) => i !== index);
  }

  function removeInlineGuardrail(index: number) {
    if (!inlineDraft) return;
    inlineDraft.guardrails = inlineDraft.guardrails.filter((_, i) => i !== index);
  }

  function removeInlinePitfall(index: number) {
    if (!inlineDraft) return;
    inlineDraft.pitfalls = inlineDraft.pitfalls.filter((_, i) => i !== index);
  }

  function removeInlineRefinement(index: number) {
    if (!inlineDraft) return;
    inlineDraft.refinements = inlineDraft.refinements.filter((_, i) => i !== index);
  }

  function addTrigger() {
    form.triggers = [...form.triggers, { direction: 'for', pattern: '' }];
  }

  function addGuardrail() {
    form.guardrails = [...form.guardrails, { severity: 'warning', text: '' }];
  }

  function addPitfall() {
    form.pitfalls = [...form.pitfalls, ''];
  }

  function addRefinement() {
    form.refinements = [...form.refinements, ''];
  }

  function addInitialAsset(root = 'references') {
    form.assets = [...form.assets, emptyAssetDraft(root)];
  }

  function removeTrigger(index: number) {
    form.triggers = form.triggers.filter((_, i) => i !== index);
  }

  function removeGuardrail(index: number) {
    form.guardrails = form.guardrails.filter((_, i) => i !== index);
  }

  function removePitfall(index: number) {
    form.pitfalls = form.pitfalls.filter((_, i) => i !== index);
  }

  function removeRefinement(index: number) {
    form.refinements = form.refinements.filter((_, i) => i !== index);
  }

  function removeInitialAsset(index: number) {
    form.assets = form.assets.filter((_, i) => i !== index);
  }

  async function importInitialAssetFile(event: Event, index: number) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const draft = form.assets[index];
    if (!draft) return;

    try {
      const content = await file.text();
      form.assets = form.assets.map((asset, i) =>
        i === index
          ? {
              ...asset,
              path: pathForImportedFile(file, asset, false),
              mime_type: file.type || asset.mime_type,
              content,
            }
          : asset,
      );
    } catch (error) {
      ui.toast(errorMessage(error, 'File import failed.'), 'error');
    } finally {
      input.value = '';
    }
  }

  async function saveSkill(event: SubmitEvent) {
    event.preventDefault();
    const payload = payloadFromForm();
    const initialAssets = form.assets
      .map(syncAssetKindFromPath)
      .filter((asset) => asset.path.trim() && asset.content.length > 0);
    if (!payload.name || !payload.procedure) {
      ui.toast('Name and procedure are required.', 'error');
      return;
    }

    saving = true;
    try {
      let saved = await api.createSkill(payload);
      if (form.create_as_package && saved?.id) {
        saved = await api.convertSkillToBundle(saved.id);
      }
      if (saved?.id) {
        for (const asset of initialAssets) {
          await api.upsertSkillAsset(saved.id, {
            path: asset.path.trim(),
            content: asset.content,
            asset_kind: asset.asset_kind,
            mime_type: asset.mime_type.trim() || undefined,
          });
        }
      }
      ui.toast('Skill created.', 'success');
      editorMode = 'idle';
      await loadSkills(saved?.id ?? selectedId);
    } catch (error) {
      ui.toast(errorMessage(error, 'Skill save failed.'), 'error');
    } finally {
      saving = false;
    }
  }

  async function saveInlineSkill() {
    const skill = selectedSkill;
    if (!skill || !inlineDraft || !inlineDraftDirty) return;
    const payload = payloadFromDraft(inlineDraft);
    if (!payload.name || !payload.procedure) {
      ui.toast('Name and procedure are required.', 'error');
      return;
    }

    inlineSaving = true;
    try {
      const saved = await api.updateSkill(skill.id, payload);
      ui.toast('Skill saved.', 'success');
      await loadSkills(saved?.id ?? skill.id);
    } catch (error) {
      ui.toast(errorMessage(error, 'Skill save failed.'), 'error');
    } finally {
      inlineSaving = false;
    }
  }

  async function deleteSelected() {
    const skill = selectedSkill;
    if (!skill) return;
    if (!window.confirm(`Delete "${skill.name}" from the active skills list?`)) return;

    deleting = true;
    try {
      await api.deleteSkill(skill.id);
      ui.toast('Skill deleted from the active list.', 'success');
      await loadSkills(null);
    } catch (error) {
      ui.toast(errorMessage(error, 'Delete failed.'), 'error');
    } finally {
      deleting = false;
    }
  }

  async function convertSelectedToBundle() {
    const skill = selectedSkill;
    if (!skill) return;
    converting = true;
    try {
      const converted = await api.convertSkillToBundle(skill.id);
      ui.toast('File support added to this skill.', 'success');
      await loadSkills(converted?.id ?? skill.id);
    } catch (error) {
      ui.toast(errorMessage(error, 'Conversion failed.'), 'error');
    } finally {
      converting = false;
    }
  }

  async function openAsset(asset: SkillAsset) {
    const skill = selectedSkill;
    if (!skill) return;
    assetLoading = true;
    assetEditorOpen = false;
    editingAssetPath = null;
    assetPreview = { ...asset, content: null };
    try {
      assetPreview = await api.skillAsset(skill.id, asset.path, 16000);
    } catch (error) {
      ui.toast(errorMessage(error, 'File failed to load.'), 'error');
    } finally {
      assetLoading = false;
    }
  }

  function startNewAsset(root = 'references') {
    assetForm = emptyAssetDraft(root);
    assetPreview = null;
    editingAssetPath = null;
    assetEditorOpen = true;
  }

  async function startEditAsset(asset: SkillAsset) {
    const skill = selectedSkill;
    if (!skill) return;
    assetLoading = true;
    assetEditorOpen = true;
    editingAssetPath = asset.path;
    assetForm = {
      path: asset.path,
      asset_kind: asset.asset_kind || assetKindFromPath(asset.path),
      mime_type: asset.mime_type || '',
      content: '',
    };
    try {
      const loaded = await api.skillAsset(skill.id, asset.path, 16000);
      assetPreview = loaded;
      assetForm = {
        path: loaded.path,
        asset_kind: loaded.asset_kind || assetKindFromPath(loaded.path),
        mime_type: loaded.mime_type || '',
        content: loaded.content || '',
      };
    } catch (error) {
      ui.toast(errorMessage(error, 'File failed to load.'), 'error');
    } finally {
      assetLoading = false;
    }
  }

  async function saveAsset(event: SubmitEvent) {
    event.preventDefault();
    const skill = selectedSkill;
    if (!skill) return;
    const payload = syncAssetKindFromPath(assetForm);
    if (!payload.path.trim()) {
      ui.toast('File path is required.', 'error');
      return;
    }

    assetSaving = true;
    try {
      const saved = await api.upsertSkillAsset(skill.id, {
        path: payload.path.trim(),
        content: payload.content,
        asset_kind: payload.asset_kind,
        mime_type: payload.mime_type.trim() || undefined,
      });
      if (editingAssetPath && editingAssetPath !== payload.path.trim()) {
        await api.deleteSkillAsset(skill.id, editingAssetPath);
      }
      ui.toast('File saved.', 'success');
      await loadSkills(skill.id);
      assetPreview = saved;
      assetForm = {
        path: saved.path,
        asset_kind: saved.asset_kind || assetKindFromPath(saved.path),
        mime_type: saved.mime_type || '',
        content: saved.content || payload.content,
      };
      editingAssetPath = saved.path;
      assetEditorOpen = true;
    } catch (error) {
      ui.toast(errorMessage(error, 'File save failed.'), 'error');
    } finally {
      assetSaving = false;
    }
  }

  async function importEditorAssetFile(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;

    try {
      const content = await file.text();
      assetForm = {
        ...assetForm,
        path: pathForImportedFile(file, assetForm, Boolean(editingAssetPath)),
        mime_type: file.type || assetForm.mime_type,
        content,
      };
    } catch (error) {
      ui.toast(errorMessage(error, 'File import failed.'), 'error');
    } finally {
      input.value = '';
    }
  }

  async function deleteEditingAsset() {
    const skill = selectedSkill;
    if (!skill || !editingAssetPath) return;
    if (!window.confirm(`Delete "${editingAssetPath}" from this skill?`)) return;

    assetDeleting = true;
    try {
      await api.deleteSkillAsset(skill.id, editingAssetPath);
      ui.toast('File deleted.', 'success');
      assetEditorOpen = false;
      editingAssetPath = null;
      assetPreview = null;
      await loadSkills(skill.id);
    } catch (error) {
      ui.toast(errorMessage(error, 'File delete failed.'), 'error');
    } finally {
      assetDeleting = false;
    }
  }

</script>

<svelte:head>
  <title>Skills</title>
</svelte:head>

<ConstellationPageFrame
  eyebrow="Agent workflows"
  title="Skills"
  subtitle={`${plural(items.length, 'workflow')} Illo can use while working.`}
  contentClassName="skills-page"
>
  {#snippet actions()}
    <ConstellationButton variant="secondary" onclick={startCreate}>
      {#snippet leadingVisual()}
        <ConstellationIcon name="edit" size={14} />
      {/snippet}
      New skill
    </ConstellationButton>
  {/snippet}

  {#if loadError}
    <ConstellationNotice title="Skills failed to load." description={loadError} tone="danger">
      {#snippet actions()}
        <ConstellationButton variant="secondary" size="sm" onclick={() => loadSkills()}>Retry</ConstellationButton>
      {/snippet}
    </ConstellationNotice>
  {/if}

  <section class="workspace">
    <section class="inventory-panel" aria-label="Skill inventory">
      <div class="inventory-tools">
        <ConstellationSearchField bind:value={search} placeholder="Search skills..." aria-label="Search skills" />
      </div>

      <div class="skill-list" aria-label="Skills">
        {#if loading}
          {#each Array(9) as _}
            <div class="skill-row-skeleton"></div>
          {/each}
        {:else if filteredItems.length === 0}
          <ConstellationEmptyState
            title="No matching skills"
            description="No skill matches the current search."
            size="sm"
            surface="plain"
          />
        {:else}
          {#each filteredItems as item (item.skill.id)}
            <article class="skill-item" class:is-expanded={selectedSkill?.id === item.skill.id}>
            <button
              type="button"
              class="skill-row"
              class:is-selected={selectedSkill?.id === item.skill.id}
              onclick={() => selectItem(item)}
            >
              <span class="skill-row-main">
                <strong>{item.skill.name}</strong>
                <small>{item.skill.description || 'No description'}</small>
              </span>
              {#if attentionReasons(item).length}
                <span class="skill-row-side">
                  <ConstellationPill variant={attentionReasons(item)[0].tone} leadingDot>{attentionReasons(item)[0].label}</ConstellationPill>
                </span>
              {/if}
            </button>
            {#if selectedSkill?.id === item.skill.id && selectedPackage}
              <div class="skill-expanded">
                <div class="expanded-toolbar">
                  <div class="expanded-facts" aria-label="Skill facts">
                    <span>{numberValue(item.skill.use_count)} times used</span>
                    <span>{array(item.skill.triggers).length} examples</span>
                    <span>{array(item.skill.guardrails).length} guardrails</span>
                    {#if selectedPackageFiles.length}
                      <span>{selectedPackageFiles.length} files</span>
                    {/if}
                  </div>
                  <div class="expanded-actions">
                    {#if inlineDraftDirty}
                      <span class="expanded-save-state">Unsaved changes</span>
                      <ConstellationButton
                        variant="quiet"
                        size="sm"
                        onclick={resetInlineDraft}
                        disabled={inlineSaving}
                      >
                        Revert
                      </ConstellationButton>
                      <ConstellationButton
                        variant="secondary"
                        size="sm"
                        onclick={saveInlineSkill}
                        loading={inlineSaving}
                        loadingLabel="Saving"
                      >
                        Save
                      </ConstellationButton>
                    {/if}
                    {#if selectedItem?.convert_to_bundle_available}
                      <ConstellationButton
                        variant="secondary"
                        size="sm"
                        onclick={convertSelectedToBundle}
                        loading={converting}
                        loadingLabel="Adding"
                      >
                        Add files
                      </ConstellationButton>
                    {/if}
                    <ConstellationButton
                      variant="destructive"
                      size="sm"
                      onclick={deleteSelected}
                      loading={deleting}
                      loadingLabel="Deleting"
                    >
                      Delete
                    </ConstellationButton>
                  </div>
                </div>
                {#if attentionReasons(item).length}
                  <div class="attention-reasons" aria-label="Why this skill needs work">
                    {#each attentionReasons(item) as reason}
                      <div class="attention-reason">
                        <ConstellationPill variant={reason.tone}>{reason.label}</ConstellationPill>
                        <span>{reason.detail}</span>
                      </div>
                    {/each}
                  </div>
                {/if}

                {#if inlineDraft}
                  <div class="inline-skill-fields" aria-label="Inline skill editor">
                    <label class="field">
                      <span>Name</span>
                      <input bind:value={inlineDraft.name} required maxlength="80" />
                    </label>
                    <label class="field">
                      <span>Description</span>
                      <input bind:value={inlineDraft.description} placeholder="When this skill should help" />
                    </label>
                  </div>
                {/if}

                <details class="skill-region" open>
                  <summary>
                    <span>Procedure</span>
                    <small>Reusable instructions</small>
                  </summary>
                  {#if inlineDraft}
                    <textarea
                      class="inline-procedure-editor"
                      bind:value={inlineDraft.procedure}
                      required
                      rows="10"
                      placeholder="Write the reusable steps here."
                      aria-label="Skill procedure"
                    ></textarea>
                  {:else}
                    <pre>{selectedSkill.procedure || 'No procedure supplied.'}</pre>
                  {/if}
                </details>

                <details class="skill-region">
                  <summary>
                    <span>Examples and guardrails</span>
                    <small>{inlineDraft?.triggers.length ?? array(selectedSkill.triggers).length} examples / {inlineDraft?.guardrails.length ?? array(selectedSkill.guardrails).length} guardrails</small>
                  </summary>
                  <div class="region-columns">
                    <section>
                      <div class="inline-section-head">
                        <h3>When to use it</h3>
                        <ConstellationButton variant="quiet" size="sm" onclick={addInlineTrigger}>Add</ConstellationButton>
                      </div>
                      {#if inlineDraft && inlineDraft.triggers.length}
                        <div class="inline-row-list">
                          {#each inlineDraft.triggers as trigger, index (index)}
                            <div class="editable-row">
                              <select bind:value={trigger.direction} aria-label="Example direction">
                                {#each TRIGGER_DIRECTIONS as direction}
                                  <option value={direction}>{triggerDirectionLabel(direction)}</option>
                                {/each}
                              </select>
                              <input bind:value={trigger.pattern} placeholder="Example request" aria-label="Example request" />
                              <button type="button" class="icon-button" title="Remove example" onclick={() => removeInlineTrigger(index)}>
                                <ConstellationIcon name="x" size={14} />
                              </button>
                            </div>
                          {/each}
                        </div>
                      {:else if inlineDraft}
                        <p class="empty-inline">No examples yet.</p>
                      {:else if array(selectedSkill.triggers).length}
                        <ul class="line-list">
                          {#each array(selectedSkill.triggers) as trigger}
                            <li><strong>{triggerDirectionLabel(trigger.direction)}</strong><span>{trigger.pattern || 'No pattern'}</span></li>
                          {/each}
                        </ul>
                      {/if}
                    </section>
                    <section>
                      <div class="inline-section-head">
                        <h3>Guardrails</h3>
                        <ConstellationButton variant="quiet" size="sm" onclick={addInlineGuardrail}>Add</ConstellationButton>
                      </div>
                      {#if inlineDraft && inlineDraft.guardrails.length}
                        <div class="inline-row-list">
                          {#each inlineDraft.guardrails as guardrail, index (index)}
                            <div class="editable-row">
                              <select bind:value={guardrail.severity} aria-label="Guardrail severity">
                                {#each GUARDRAIL_SEVERITIES as severity}
                                  <option value={severity}>{severity}</option>
                                {/each}
                              </select>
                              <input bind:value={guardrail.text} placeholder="Guardrail text" aria-label="Guardrail text" />
                              <button type="button" class="icon-button" title="Remove guardrail" onclick={() => removeInlineGuardrail(index)}>
                                <ConstellationIcon name="x" size={14} />
                              </button>
                            </div>
                          {/each}
                        </div>
                      {:else if inlineDraft}
                        <p class="empty-inline">No guardrails yet.</p>
                      {:else if array(selectedSkill.guardrails).length}
                        <ul class="line-list">
                          {#each array(selectedSkill.guardrails) as guardrail}
                            <li><strong>{guardrail.severity || 'warning'}</strong><span>{guardrail.text || 'No text'}</span></li>
                          {/each}
                        </ul>
                      {/if}
                    </section>
                  </div>
                </details>

                <details class="skill-region">
                  <summary>
                    <span>Health notes</span>
                    <small>{textItems(selectedSkill.refinements).length + textItems(selectedSkill.pitfalls).length} notes</small>
                  </summary>
                  <div class="region-columns">
                    <section>
                      <h3>Workflow health</h3>
                      {#if advisoryItems(selectedSkill).length}
                        <div class="advisory-list" aria-label="Skill notes">
                          {#each advisoryItems(selectedSkill) as note}
                            <div class="advisory-row">
                              <ConstellationPill variant={note.tone}>{note.label}</ConstellationPill>
                              <span>{note.detail}</span>
                            </div>
                          {/each}
                        </div>
                      {:else}
                        <p class="empty-inline">No active advisories.</p>
                      {/if}
                    </section>
                    <section>
                      <h3>Notes</h3>
                      {#if inlineDraft}
                        <div class="inline-note-groups">
                          <div class="inline-note-group">
                            <div class="inline-section-head">
                              <strong>Avoid</strong>
                              <ConstellationButton variant="quiet" size="sm" onclick={addInlinePitfall}>Add</ConstellationButton>
                            </div>
                            {#if inlineDraft.pitfalls.length}
                              <div class="inline-row-list">
                                {#each inlineDraft.pitfalls as _, index (index)}
                                  <div class="editable-row text-only">
                                    <input bind:value={inlineDraft.pitfalls[index]} placeholder="Thing to avoid" aria-label="Thing to avoid" />
                                    <button type="button" class="icon-button" title="Remove note" onclick={() => removeInlinePitfall(index)}>
                                      <ConstellationIcon name="x" size={14} />
                                    </button>
                                  </div>
                                {/each}
                              </div>
                            {:else}
                              <p class="empty-inline">No avoid notes.</p>
                            {/if}
                          </div>
                          <div class="inline-note-group">
                            <div class="inline-section-head">
                              <strong>Improve</strong>
                              <ConstellationButton variant="quiet" size="sm" onclick={addInlineRefinement}>Add</ConstellationButton>
                            </div>
                            {#if inlineDraft.refinements.length}
                              <div class="inline-row-list">
                                {#each inlineDraft.refinements as _, index (index)}
                                  <div class="editable-row text-only">
                                    <input bind:value={inlineDraft.refinements[index]} placeholder="Improvement" aria-label="Improvement" />
                                    <button type="button" class="icon-button" title="Remove improvement" onclick={() => removeInlineRefinement(index)}>
                                      <ConstellationIcon name="x" size={14} />
                                    </button>
                                  </div>
                                {/each}
                              </div>
                            {:else}
                              <p class="empty-inline">No improvements.</p>
                            {/if}
                          </div>
                        </div>
                      {:else if textItems(selectedSkill.pitfalls).length || textItems(selectedSkill.refinements).length}
                        {#if textItems(selectedSkill.pitfalls).length}
                          <ul class="line-list compact-list">
                            {#each textItems(selectedSkill.pitfalls) as pitfall}
                              <li><strong>Avoid</strong><span>{pitfall}</span></li>
                            {/each}
                          </ul>
                        {/if}
                        {#if textItems(selectedSkill.refinements).length}
                          <ul class="line-list compact-list">
                            {#each textItems(selectedSkill.refinements) as refinement}
                              <li><strong>Improve</strong><span>{refinement}</span></li>
                            {/each}
                          </ul>
                        {/if}
                      {:else}
                        <p class="empty-inline">No notes yet.</p>
                      {/if}
                    </section>
                  </div>
                </details>

                <details class="skill-region">
                  <summary>
                    <span>Files</span>
                    <small>Optional references, examples, templates, and evals</small>
                  </summary>
                  <div class="files-region">
                    <section class="files-panel">
                      <h3>Attached files</h3>
                      <div class="file-tree" aria-label="Skill files">
                        {#each selectedAssetGroups as group}
                          <section class="file-folder">
                            <div class="file-folder-row">
                              <span>
                                <ConstellationIcon name={group.icon} size={15} />
                                <strong>{group.root}/</strong>
                              </span>
                              <button type="button" class="mini-action" onclick={() => startNewAsset(group.root)}>
                                Add
                              </button>
                            </div>
                            {#if group.assets.length}
                              <ul>
                                {#each group.assets as asset}
                                  <li>
                                    <button type="button" onclick={() => startEditAsset(asset)}>
                                      <ConstellationIcon name="document" size={14} />
                                      <span>
                                        <strong>{asset.path.split('/').slice(1).join('/') || asset.path}</strong>
                                        <small>{asset.asset_kind || group.kind} / {formatBytes(asset.size_bytes)}</small>
                                      </span>
                                    </button>
                                  </li>
                                {/each}
                              </ul>
                            {:else}
                              <p class="empty-inline">No {group.label.toLowerCase()} files.</p>
                            {/if}
                          </section>
                        {/each}
                      </div>

                      {#if assetEditorOpen}
                        <section class="asset-inline-editor">
                          <div class="section-head">
                            <div>
                              <h3>{editingAssetPath ? editingAssetPath : 'New file'}</h3>
                              <p>{assetForm.asset_kind}</p>
                            </div>
                            {#if editingAssetPath}
                              <ConstellationButton
                                variant="destructive"
                                size="sm"
                                onclick={deleteEditingAsset}
                                loading={assetDeleting}
                                loadingLabel="Deleting"
                              >
                                Delete file
                              </ConstellationButton>
                            {/if}
                          </div>
                          {#if assetLoading}
                            <ConstellationSkeletonBlock variant="panel" height="180px" />
                          {:else}
                            <form class="asset-form" onsubmit={saveAsset}>
                              <div class="form-grid two">
                                <label class="field">
                                  <span>Path</span>
                                  <input
                                    bind:value={assetForm.path}
                                    placeholder="references/context.md"
                                    oninput={() => (assetForm.asset_kind = assetKindFromPath(assetForm.path))}
                                  />
                                </label>
                                <div class="form-grid two compact">
                                  <label class="field">
                                    <span>Kind</span>
                                    <select
                                      bind:value={assetForm.asset_kind}
                                      onchange={() => (assetForm.path = assetSampleForKind(assetForm.asset_kind))}
                                    >
                                      {#each ASSET_ROOTS as option}
                                        <option value={option.kind}>{option.kind}</option>
                                      {/each}
                                    </select>
                                  </label>
                                  <label class="field">
                                    <span>MIME</span>
                                    <input bind:value={assetForm.mime_type} placeholder="auto" />
                                  </label>
                                </div>
                              </div>
                              <label class="field">
                                <span>Content</span>
                                <textarea bind:value={assetForm.content} rows="10" placeholder="File content"></textarea>
                              </label>
                              <div class="form-actions">
                                <label class="mini-action file-action">
                                  Choose file
                                  <input type="file" accept={TEXT_ASSET_ACCEPT} onchange={importEditorAssetFile} />
                                </label>
                                <ConstellationButton type="submit" loading={assetSaving} loadingLabel="Saving">
                                  Save file
                                </ConstellationButton>
                                <ConstellationButton
                                  variant="quiet"
                                  onclick={() => {
                                    assetEditorOpen = false;
                                    editingAssetPath = null;
                                  }}
                                  disabled={assetSaving}
                                >
                                  Cancel
                                </ConstellationButton>
                              </div>
                            </form>
                          {/if}
                        </section>
                      {:else if assetPreview}
                        <section class="asset-inline-editor">
                          <div class="section-head">
                            <div>
                              <h3>{assetPreview.path}</h3>
                              <p>{assetPreview.mime_type || 'text/plain'}</p>
                            </div>
                            <ConstellationPill variant="muted">{assetPreview.asset_kind || 'file'}</ConstellationPill>
                          </div>
                          {#if assetLoading}
                            <ConstellationSkeletonBlock variant="panel" height="140px" />
                          {:else}
                            <pre>{assetPreview.content || 'No inline content for this file.'}</pre>
                          {/if}
                        </section>
                      {/if}
                    </section>

                  </div>
                </details>
              </div>
            {/if}
            </article>
          {/each}
        {/if}
      </div>
    </section>

    {#if editorMode !== 'idle'}
      <ConstellationPanel className="editor-panel" ariaLabel="Skill editor">
        {#snippet header()}
          <ConstellationSectionHeader
            eyebrow="Create"
            title="New skill"
            description="Define how Illo recognizes and follows this workflow."
            size="sm"
          >
            {#snippet actions()}
              <ConstellationButton variant="quiet" size="sm" onclick={cancelEdit} disabled={saving}>
                Cancel
              </ConstellationButton>
            {/snippet}
          </ConstellationSectionHeader>
        {/snippet}

        <form class="skill-form" onsubmit={saveSkill}>
          <label class="field">
            <span>Name</span>
            <input bind:value={form.name} required maxlength="80" placeholder="code-review" />
          </label>

          <label class="field">
            <span>Description</span>
            <input bind:value={form.description} placeholder="When this skill should help" />
          </label>

          <label class="field">
            <span>Procedure</span>
            <textarea bind:value={form.procedure} required rows="12" placeholder="Write the reusable steps here."></textarea>
          </label>

          <label class="toggle-row">
            <input type="checkbox" bind:checked={form.create_as_package} />
            <span>Allow files with this skill</span>
          </label>

          <section class="editor-section">
            <div class="section-head">
              <div>
                <h3>When to use it</h3>
                <p>Example requests that should call this skill.</p>
              </div>
              <ConstellationButton variant="quiet" size="sm" onclick={addTrigger}>Add</ConstellationButton>
            </div>
            <div class="row-editor">
              {#if form.triggers.length === 0}
                <p class="empty-inline">No examples yet.</p>
              {/if}
              {#each form.triggers as trigger, index (index)}
                <div class="editable-row">
                  <select bind:value={trigger.direction} aria-label="Example direction">
                    {#each TRIGGER_DIRECTIONS as direction}
                      <option value={direction}>{triggerDirectionLabel(direction)}</option>
                    {/each}
                  </select>
                  <input bind:value={trigger.pattern} placeholder="Example request" aria-label="Example request" />
                  <button type="button" class="icon-button" title="Remove example" onclick={() => removeTrigger(index)}>
                    <ConstellationIcon name="x" size={14} />
                  </button>
                </div>
              {/each}
            </div>
          </section>

          <section class="editor-section">
            <div class="section-head">
              <div>
                <h3>Guardrails</h3>
                <p>Checks the skill should remember before it acts.</p>
              </div>
              <ConstellationButton variant="quiet" size="sm" onclick={addGuardrail}>Add</ConstellationButton>
            </div>
            <div class="row-editor">
              {#if form.guardrails.length === 0}
                <p class="empty-inline">No guardrails yet.</p>
              {/if}
              {#each form.guardrails as guardrail, index (index)}
                <div class="editable-row">
                  <select bind:value={guardrail.severity} aria-label="Guardrail severity">
                    {#each GUARDRAIL_SEVERITIES as severity}
                      <option value={severity}>{severity}</option>
                    {/each}
                  </select>
                  <input bind:value={guardrail.text} placeholder="Guardrail text" aria-label="Guardrail text" />
                  <button type="button" class="icon-button" title="Remove guardrail" onclick={() => removeGuardrail(index)}>
                    <ConstellationIcon name="x" size={14} />
                  </button>
                </div>
              {/each}
            </div>
          </section>

          <section class="editor-section">
            <div class="section-head">
              <div>
                <h3>Files</h3>
                <p>Optional references, examples, templates, and evals.</p>
              </div>
              <div class="asset-root-actions">
                {#each ASSET_ROOTS as root}
                  <button type="button" class="mini-action" onclick={() => addInitialAsset(root.root)}>
                    {root.label}
                  </button>
                {/each}
              </div>
            </div>
            <div class="asset-draft-list">
              {#if form.assets.length === 0}
                <p class="empty-inline">No files yet.</p>
              {/if}
              {#each form.assets as asset, index (index)}
                <div class="asset-draft-card">
                  <div class="form-grid two">
                    <label class="field">
                      <span>Path</span>
                      <input
                        bind:value={asset.path}
                        placeholder="references/context.md"
                        oninput={() => (asset.asset_kind = assetKindFromPath(asset.path))}
                      />
                    </label>
                    <div class="form-grid two compact">
                      <label class="field">
                        <span>Kind</span>
                        <select
                          bind:value={asset.asset_kind}
                          onchange={() => (asset.path = assetSampleForKind(asset.asset_kind))}
                        >
                          {#each ASSET_ROOTS as option}
                            <option value={option.kind}>{option.kind}</option>
                          {/each}
                        </select>
                      </label>
                      <label class="field">
                        <span>MIME</span>
                        <input bind:value={asset.mime_type} placeholder="auto" />
                      </label>
                    </div>
                  </div>
                  <label class="field">
                    <span>Content</span>
                    <textarea bind:value={asset.content} rows="8" placeholder="File content"></textarea>
                  </label>
                  <div class="asset-card-actions">
                    <label class="mini-action file-action">
                      Choose file
                      <input
                        type="file"
                        accept={TEXT_ASSET_ACCEPT}
                        onchange={(event) => importInitialAssetFile(event, index)}
                      />
                    </label>
                    <ConstellationButton variant="quiet" size="sm" onclick={() => removeInitialAsset(index)}>
                      Remove
                    </ConstellationButton>
                  </div>
                </div>
              {/each}
            </div>
          </section>

          <div class="form-grid two">
            <section class="editor-section">
              <div class="section-head">
                <div>
                  <h3>Avoid</h3>
                  <p>Patterns the skill should avoid.</p>
                </div>
                <ConstellationButton variant="quiet" size="sm" onclick={addPitfall}>Add</ConstellationButton>
              </div>
              <div class="row-editor">
                {#if form.pitfalls.length === 0}
                  <p class="empty-inline">No notes yet.</p>
                {/if}
                {#each form.pitfalls as _, index (index)}
                  <div class="editable-row text-only">
                    <input bind:value={form.pitfalls[index]} placeholder="Thing to avoid" aria-label="Thing to avoid" />
                    <button type="button" class="icon-button" title="Remove note" onclick={() => removePitfall(index)}>
                      <ConstellationIcon name="x" size={14} />
                    </button>
                  </div>
                {/each}
              </div>
            </section>

            <section class="editor-section">
              <div class="section-head">
                <div>
                  <h3>Improvements</h3>
                  <p>Useful improvements.</p>
                </div>
                <ConstellationButton variant="quiet" size="sm" onclick={addRefinement}>Add</ConstellationButton>
              </div>
              <div class="row-editor">
                {#if form.refinements.length === 0}
                  <p class="empty-inline">No improvements yet.</p>
                {/if}
                {#each form.refinements as _, index (index)}
                  <div class="editable-row text-only">
                    <input bind:value={form.refinements[index]} placeholder="Improvement" aria-label="Improvement" />
                    <button type="button" class="icon-button" title="Remove improvement" onclick={() => removeRefinement(index)}>
                      <ConstellationIcon name="x" size={14} />
                    </button>
                  </div>
                {/each}
              </div>
            </section>
          </div>

          <div class="form-actions">
            <ConstellationButton type="submit" loading={saving} loadingLabel="Saving">
              Save skill
            </ConstellationButton>
            <ConstellationButton variant="quiet" onclick={cancelEdit} disabled={saving}>Cancel</ConstellationButton>
          </div>
        </form>
      </ConstellationPanel>
    {/if}
  </section>
</ConstellationPageFrame>

<style>
  :global(.skills-page) {
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
    gap: 12px;
    min-height: 0;
    min-width: 0;
  }

  :global(.editor-panel .constellation-panel-content) {
    max-height: calc(100vh - 260px);
    overflow: auto;
  }

  :global(.editor-panel) {
    order: -1;
  }

  .inventory-tools {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    padding: 0;
  }

  .inventory-tools :global(.constellation-search-field) {
    flex: 1 1 260px;
  }

  .skill-list {
    display: grid;
    align-content: start;
    gap: 8px;
    min-height: 0;
    overflow: visible;
    padding: 0;
  }

  .skill-item {
    display: grid;
    min-width: 0;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: var(--constellation-surface-panel-background);
    overflow: hidden;
  }

  .skill-item.is-expanded {
    border-color: var(--constellation-control-focus-ring);
  }

  .skill-row,
  .skill-row-skeleton {
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

  .skill-row {
    cursor: pointer;
    transition:
      border-color var(--constellation-motion-settle-duration) ease,
      background-color var(--constellation-motion-settle-duration) ease,
      transform var(--constellation-motion-hover-duration) ease;
  }

  .skill-row:hover,
  .skill-row.is-selected {
    background: color-mix(in srgb, var(--constellation-color-text-primary) 3%, transparent);
  }

  .skill-row-main,
  .skill-row-side {
    display: grid;
    min-width: 0;
    gap: 7px;
  }

  .skill-row-main strong,
  .skill-row-main small {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .skill-row-main strong {
    font-size: 13px;
    font-weight: 560;
    letter-spacing: 0;
  }

  .skill-row-main small,
  .empty-inline,
  .section-head p {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.5;
  }

  .skill-row-side {
    justify-items: end;
  }

  .skill-row-skeleton {
    background:
      linear-gradient(90deg, transparent, var(--constellation-skeleton-row-shimmer), transparent),
      var(--constellation-skeleton-row-background);
    background-size: 200% 100%;
    animation: skills-pulse 1.4s ease-in-out infinite;
  }

  .skill-expanded {
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
  .expanded-actions {
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

  .expanded-save-state {
    color: var(--constellation-color-text-tertiary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .attention-reasons {
    display: grid;
    gap: 6px;
    padding: 9px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-color-text-primary) 2.5%, transparent);
  }

  .attention-reason {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 9px;
    min-width: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .inline-skill-fields {
    display: grid;
    grid-template-columns: minmax(160px, 0.42fr) minmax(0, 1fr);
    gap: 12px;
    padding-top: 2px;
  }

  .skill-region {
    display: grid;
    min-width: 0;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: var(--constellation-surface-panel-background);
  }

  .skill-region summary {
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

  .skill-region summary::-webkit-details-marker {
    display: none;
  }

  .skill-region summary span {
    font-size: 13px;
    font-weight: 560;
  }

  .skill-region summary small {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .skill-region[open] summary {
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .skill-region > :not(summary) {
    margin: 12px;
  }

  .region-columns {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 16px;
  }

  .files-region {
    display: grid;
    gap: 16px;
  }

  .inline-section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    min-height: 30px;
  }

  .inline-section-head h3,
  .inline-section-head strong {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-size: 12px;
    font-weight: 560;
    letter-spacing: 0;
  }

  .inline-row-list,
  .inline-note-groups,
  .inline-note-group {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .region-columns section,
  .file-folder,
  .files-panel {
    min-width: 0;
  }

  .region-columns h3,
  .files-region h3,
  .file-folder-row strong {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-size: 12px;
    font-weight: 560;
    letter-spacing: 0;
  }

  .file-tree {
    display: grid;
    gap: 8px;
  }

  .file-folder {
    display: grid;
    gap: 6px;
    padding-left: 10px;
    border-left: 1px solid var(--constellation-surface-panel-separator);
  }

  .file-folder-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    min-height: 30px;
  }

  .file-folder-row span {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .file-folder ul {
    display: grid;
    gap: 4px;
    margin: 0;
    padding: 0 0 0 20px;
    list-style: none;
  }

  .file-folder li button {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 8px;
    width: 100%;
    min-height: 34px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--constellation-color-text-primary);
    text-align: left;
    cursor: pointer;
  }

  .file-folder li button:hover {
    background: var(--constellation-control-button-secondary-background-hover);
  }

  .file-folder li span {
    display: grid;
    min-width: 0;
  }

  .file-folder li strong {
    overflow: hidden;
    font-size: 13px;
    font-weight: 520;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-folder li small {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .asset-inline-editor {
    display: grid;
    gap: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .skill-form,
  .asset-form,
  .row-editor,
  .asset-draft-list {
    display: grid;
    gap: 14px;
    min-width: 0;
  }

  .form-grid {
    display: grid;
    gap: 12px;
  }

  .form-grid.two {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }

  .form-grid.compact {
    gap: 10px;
  }

  .field {
    display: grid;
    gap: 7px;
    min-width: 0;
  }

  .field span,
  .section-head h3 {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-size: 12px;
    font-weight: 560;
    letter-spacing: 0;
  }

  .field span {
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    text-transform: uppercase;
    letter-spacing: 0.14em;
  }

  input,
  select,
  textarea {
    width: 100%;
    min-width: 0;
    border-radius: 8px;
    border: 1px solid var(--constellation-control-field-border);
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-primary);
    font: inherit;
    font-size: 13px;
    letter-spacing: 0;
    outline: none;
  }

  input,
  select {
    min-height: 36px;
    padding: 0 10px;
  }

  textarea {
    min-height: 220px;
    resize: vertical;
    padding: 12px;
    line-height: 1.55;
  }

  .inline-procedure-editor {
    min-height: 240px;
    border: 0;
    border-radius: 0;
    background: transparent;
    font-family: var(--constellation-font-mono);
    font-size: 12px;
    line-height: 1.6;
    padding: 0;
  }

  .inline-procedure-editor:focus {
    outline-offset: 4px;
  }

  input:focus,
  select:focus,
  textarea:focus {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .toggle-row {
    display: flex;
    align-items: center;
    gap: 10px;
    width: max-content;
    color: var(--constellation-color-text-secondary);
    font-size: 13px;
  }

  .toggle-row input {
    width: 16px;
    min-height: 16px;
  }

  .editor-section {
    display: grid;
    gap: 12px;
    min-width: 0;
    padding-top: 16px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .section-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 14px;
  }

  .section-head div {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .asset-root-actions,
  .asset-card-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
  }

  .mini-action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 28px;
    padding: 0 10px;
    border-radius: 8px;
    border: 1px solid var(--constellation-control-button-secondary-border);
    background: var(--constellation-control-button-secondary-background);
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    cursor: pointer;
  }

  .file-action {
    position: relative;
    overflow: hidden;
  }

  .file-action input {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
  }

  .mini-action:hover {
    border-color: var(--constellation-control-focus-ring);
    color: var(--constellation-color-text-primary);
  }

  .section-head p,
  .empty-inline {
    margin: 0;
  }

  .editable-row {
    display: grid;
    grid-template-columns: minmax(104px, 128px) minmax(0, 1fr) 36px;
    gap: 8px;
    align-items: center;
  }

  .editable-row.text-only {
    grid-template-columns: minmax(0, 1fr) 36px;
  }

  .asset-draft-card {
    display: grid;
    gap: 12px;
    min-width: 0;
    padding: 12px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
  }

  .icon-button {
    display: inline-grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border-radius: 8px;
    border: 1px solid var(--constellation-control-button-secondary-border);
    background: var(--constellation-control-button-secondary-background);
    color: var(--constellation-color-text-secondary);
    cursor: pointer;
  }

  .icon-button:hover {
    border-color: var(--constellation-button-destructive-border);
    color: var(--constellation-button-destructive-text);
  }

  .form-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    justify-content: flex-end;
    padding-top: 4px;
  }

  .advisory-list {
    display: grid;
    gap: 8px;
  }

  .advisory-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 10px;
    min-width: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  pre {
    max-height: 340px;
    margin: 0;
    overflow: auto;
    padding: 14px;
    border-radius: 8px;
    border: 1px solid var(--constellation-surface-nested-border);
    background: var(--constellation-surface-nested-background);
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-mono);
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
  }

  .line-list {
    display: grid;
    gap: 8px;
    margin: 0;
    padding: 0;
  }

  .line-list {
    list-style: none;
  }

  .line-list li {
    display: grid;
    gap: 5px;
    min-width: 0;
    padding: 10px 0;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .compact-list + .compact-list {
    margin-top: 8px;
  }

  .line-list span {
    min-width: 0;
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    font-size: 13px;
    line-height: 1.45;
  }

  .line-list strong {
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    text-transform: uppercase;
    letter-spacing: 0.14em;
  }

  @keyframes skills-pulse {
    from {
      background-position: 200% 0;
    }
    to {
      background-position: -200% 0;
    }
  }

  @media (max-width: 820px) {
    .workspace {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 760px) {
    .inline-skill-fields,
    .region-columns,
    .form-grid.two {
      grid-template-columns: 1fr;
    }

    .editable-row {
      grid-template-columns: 1fr 36px;
    }

    .editable-row select {
      grid-column: 1 / -1;
    }

    .section-head {
      flex-direction: column;
    }
  }
</style>
