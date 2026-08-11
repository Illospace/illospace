<script lang="ts">
  import { onMount, tick } from 'svelte';

  import {
    ConstellationButton,
    ConstellationSelect,
    ConstellationTextInput,
    ConstellationTextarea,
  } from '$lib/components/constellation';
  import {
    clonePolicyDraft,
    POLICY_FIELD_SCHEMA,
    type CyclePolicyDraft,
    type CyclePolicyDraftErrors,
    type CyclePolicyFieldKey,
  } from '$lib/features/cycles/domain/effectivePolicy';
  import type { RuntimeModelCatalogEntry } from '$lib/types/runtimeSettings';

  let {
    cycleId,
    draft,
    errors,
    error,
    dirty,
    reviewing,
    compact = false,
    modelCatalog,
    onDraftChange,
    onCancel,
    onReview,
  }: {
    cycleId: number;
    draft: CyclePolicyDraft;
    errors: CyclePolicyDraftErrors;
    error?: string;
    dirty: boolean;
    reviewing: boolean;
    compact?: boolean;
    modelCatalog: readonly RuntimeModelCatalogEntry[];
    onDraftChange: (draft: CyclePolicyDraft) => void;
    onCancel: () => void;
    onReview: () => void;
  } = $props();

  const modelOptions = $derived([
    { value: '', label: 'Workspace default', description: 'Use the workspace model' },
    ...modelCatalog.map((entry) => ({
      value: entry.id,
      label: entry.label,
      description: entry.description,
    })),
    ...(draft.model_override && !modelCatalog.some((entry) => entry.id === draft.model_override)
      ? [{
          value: draft.model_override,
          label: draft.model_override,
          description: 'Current model selection',
        }]
      : []),
  ]);

  function inputValue(event: Event): string {
    return (event.currentTarget as HTMLInputElement | HTMLTextAreaElement).value;
  }

  function changeField<Key extends CyclePolicyFieldKey>(
    key: Key,
    value: CyclePolicyDraft[Key],
  ): void {
    const next = clonePolicyDraft(draft);
    Object.assign(next, { [key]: value });
    onDraftChange(next);
  }

  function addGuidance(): void {
    changeField('guidance', [...draft.guidance, '']);
  }

  function updateGuidance(index: number, value: string): void {
    const guidance = [...draft.guidance];
    guidance[index] = value;
    changeField('guidance', guidance);
  }

  function removeGuidance(index: number): void {
    changeField('guidance', draft.guidance.filter((_, itemIndex) => itemIndex !== index));
  }

  function fieldControlId(key: CyclePolicyFieldKey): string {
    return `cycle-${cycleId}-policy-${key}`;
  }

  function fieldErrorId(key: CyclePolicyFieldKey): string {
    return `${fieldControlId(key)}-error`;
  }

  function guidanceControlId(index: number): string {
    return `${fieldControlId('guidance')}-${index + 1}`;
  }

  function focusControl(id: string): void {
    tick().then(() => document.getElementById(id)?.focus());
  }

  onMount(() => {
    focusControl(fieldControlId(POLICY_FIELD_SCHEMA[0].key));
  });

  $effect(() => {
    const firstInvalidField = POLICY_FIELD_SCHEMA.find((field) => Boolean(errors[field.key]));
    if (!firstInvalidField) return;
    focusControl(
      firstInvalidField.key === 'guidance'
        ? guidanceControlId(0)
        : fieldControlId(firstInvalidField.key),
    );
  });
</script>

<section class:compact class="policy-editor" aria-label="Edit cycle behavior">
  <header class="editor-heading">
    <div>
      <span class="section-kicker">Draft</span>
      <h4>Prepare behavior change</h4>
    </div>
    <p>Nothing changes until you review and apply.</p>
  </header>

  {#if error}<p class="editor-error" role="alert">{error}</p>{/if}

  <div class="editor-fields">
    {#each POLICY_FIELD_SCHEMA as field (field.key)}
      {#if field.control === 'textarea'}
        <div class="editor-field" class:editor-field-full={field.fullWidth}>
          <label for={fieldControlId(field.key)}>{field.label}</label>
          <ConstellationTextarea
            id={fieldControlId(field.key)}
            value={draft[field.key]}
            rows={field.rows}
            aria-invalid={errors[field.key] ? 'true' : undefined}
            aria-describedby={errors[field.key] ? fieldErrorId(field.key) : undefined}
            oninput={(event) => changeField(field.key, inputValue(event))}
          />
          {#if errors[field.key]}<small id={fieldErrorId(field.key)} class="field-error">{errors[field.key]}</small>{/if}
        </div>
      {:else if field.control === 'text'}
        <div class="editor-field">
          <label for={fieldControlId(field.key)}>{field.label}</label>
          <ConstellationTextInput
            id={fieldControlId(field.key)}
            value={draft[field.key]}
            mono={'mono' in field && field.mono}
            placeholder={field.placeholder}
            aria-invalid={errors[field.key] ? 'true' : undefined}
            aria-describedby={errors[field.key] ? fieldErrorId(field.key) : undefined}
            oninput={(event) => changeField(field.key, inputValue(event))}
          />
          {#if errors[field.key]}<small id={fieldErrorId(field.key)} class="field-error">{errors[field.key]}</small>{/if}
        </div>
      {:else if field.control === 'toggle'}
        <div class="editor-field">
          <span class="editor-field-label">{field.label}</span>
          <button
            type="button"
            class="policy-status-toggle"
            class:is-enabled={draft[field.key]}
            aria-label={`${field.label}: ${draft[field.key] ? 'Enabled' : 'Paused'}`}
            aria-pressed={draft[field.key]}
            onclick={() => changeField(field.key, !draft[field.key])}
          >
            <span aria-hidden="true"></span>
            <strong>{draft[field.key] ? 'Enabled' : 'Paused'}</strong>
          </button>
        </div>
      {:else if field.control === 'model'}
        <div class="editor-field">
          <span class="editor-field-label">{field.label}</span>
          <ConstellationSelect
            id={fieldControlId(field.key)}
            value={draft[field.key]}
            options={modelOptions}
            ariaLabel={field.label}
            ariaInvalid={errors[field.key] ? 'true' : undefined}
            ariaDescribedby={errors[field.key] ? fieldErrorId(field.key) : undefined}
            onValueChange={(value) => changeField(field.key, value)}
          />
          {#if errors[field.key]}<small id={fieldErrorId(field.key)} class="field-error">{errors[field.key]}</small>{/if}
        </div>
      {:else if field.control === 'thinking'}
        <div class="editor-field">
          <span class="editor-field-label">{field.label}</span>
          <ConstellationSelect
            id={fieldControlId(field.key)}
            value={draft[field.key]}
            options={field.options}
            ariaLabel={field.label}
            onValueChange={(value) => changeField(field.key, value)}
          />
        </div>
      {:else if field.control === 'guidance'}
        <section
          class="guidance-editor editor-field-full"
          aria-labelledby={`cycle-${cycleId}-guidance-editor`}
          aria-describedby={errors[field.key] ? fieldErrorId(field.key) : undefined}
        >
          <div class="guidance-editor-heading">
            <div>
              <span class="editor-field-label" id={`cycle-${cycleId}-guidance-editor`}>{field.label}</span>
              <small>Changing text retires the old guidance and adds a new entry.</small>
            </div>
            <ConstellationButton variant="quiet" size="sm" onclick={addGuidance}>
              Add guidance
            </ConstellationButton>
          </div>
          {#if draft[field.key].length}
            <ol class="guidance-editor-list">
              {#each draft[field.key] as guidance, index}
                <li>
                  <ConstellationTextarea
                    id={guidanceControlId(index)}
                    value={guidance}
                    rows={2}
                    aria-label={`Guidance ${index + 1}`}
                    aria-invalid={errors[field.key] ? 'true' : undefined}
                    aria-describedby={errors[field.key] ? fieldErrorId(field.key) : undefined}
                    oninput={(event) => updateGuidance(index, inputValue(event))}
                  />
                  <ConstellationButton
                    variant="quiet"
                    size="sm"
                    aria-label={`Remove guidance ${index + 1}`}
                    onclick={() => removeGuidance(index)}
                  >
                    Remove
                  </ConstellationButton>
                </li>
              {/each}
            </ol>
          {:else}
            <p class="empty-policy-value">No active guidance.</p>
          {/if}
          {#if errors[field.key]}<small id={fieldErrorId(field.key)} class="field-error">{errors[field.key]}</small>{/if}
        </section>
      {/if}
    {/each}
  </div>

  <div class="editor-actions">
    <ConstellationButton variant="quiet" size="sm" onclick={onCancel}>Cancel</ConstellationButton>
    <ConstellationButton
      variant="primary"
      size="sm"
      loading={reviewing}
      disabled={!dirty}
      onclick={onReview}
    >
      Review change
    </ConstellationButton>
  </div>
</section>

<style>
  .policy-editor {
    display: grid;
    gap: 16px;
    min-width: 0;
    padding: 16px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
    background: color-mix(in srgb, var(--constellation-color-info) 3%, transparent);
  }

  .editor-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .editor-heading h4,
  .editor-heading p,
  .guidance-editor-heading small,
  .editor-error {
    margin: 0;
  }

  .section-kicker,
  .editor-field > label,
  .editor-field-label {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    line-height: 1.2;
    text-transform: uppercase;
  }

  .section-kicker {
    letter-spacing: 0.12em;
  }

  .editor-heading h4 {
    margin-top: 4px;
    font-size: 15px;
    font-weight: 650;
  }

  .editor-heading p {
    max-width: 360px;
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    line-height: 1.5;
    text-align: right;
  }

  .editor-fields {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
  }

  .editor-field {
    display: grid;
    align-content: start;
    gap: 7px;
    min-width: 0;
  }

  .editor-field-full {
    grid-column: 1 / -1;
  }

  .editor-field > label,
  .editor-field-label {
    color: var(--constellation-color-text-secondary);
    letter-spacing: 0.08em;
  }

  .editor-field :global(.constellation-text-input),
  .editor-field :global(.constellation-textarea),
  .editor-field :global(.constellation-select),
  .guidance-editor-list :global(.constellation-textarea) {
    width: 100%;
  }

  .field-error,
  .editor-error {
    color: var(--constellation-color-danger);
    font-size: 11px;
    line-height: 1.4;
  }

  .policy-status-toggle {
    appearance: none;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 9px;
    min-height: 40px;
    padding: 6px 10px;
    border: 1px solid var(--constellation-control-field-border);
    border-radius: 8px;
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-primary);
    cursor: pointer;
  }

  .policy-status-toggle > span {
    width: 10px;
    height: 10px;
    border-radius: 999px;
    background: var(--constellation-color-text-muted);
  }

  .policy-status-toggle.is-enabled > span {
    background: var(--constellation-color-success);
    box-shadow: 0 0 10px color-mix(in srgb, var(--constellation-color-success) 55%, transparent);
  }

  .policy-status-toggle strong {
    font-size: 12px;
    font-weight: 600;
    text-align: left;
  }

  .policy-status-toggle:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .guidance-editor {
    display: grid;
    gap: 10px;
    min-width: 0;
    padding-top: 2px;
  }

  .guidance-editor-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  .guidance-editor-heading > div {
    display: grid;
    gap: 4px;
  }

  .guidance-editor-heading small {
    color: var(--constellation-color-text-muted);
    font-size: 10px;
    line-height: 1.4;
  }

  .guidance-editor-list {
    display: grid;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .guidance-editor-list li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    gap: 8px;
  }

  .empty-policy-value {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .editor-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    padding-top: 12px;
    border-top: 1px solid var(--constellation-section-divider);
  }

  .policy-editor.compact .editor-fields {
    grid-template-columns: 1fr;
  }

  .policy-editor.compact .editor-heading {
    flex-direction: column;
  }

  .policy-editor.compact .editor-heading p {
    max-width: none;
    text-align: left;
  }

  @media (max-width: 720px) {
    .editor-heading {
      align-items: flex-start;
      flex-direction: column;
    }

    .editor-heading p {
      max-width: none;
      text-align: left;
    }

    .editor-fields,
    .guidance-editor-list li {
      grid-template-columns: 1fr;
    }

    .guidance-editor-heading {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
