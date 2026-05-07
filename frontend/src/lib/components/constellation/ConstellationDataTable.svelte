<script lang="ts">
  import type { Snippet } from 'svelte';

  import ConstellationEmptyState from './ConstellationEmptyState.svelte';

  export type ConstellationDataTableAlign = 'start' | 'center' | 'end';
  export type ConstellationDataTableDensity = 'comfortable' | 'compact';
  export type ConstellationDataTableTone = 'default' | 'info' | 'success' | 'warning' | 'danger';
  export type ConstellationDataTableCellTone =
    | 'default'
    | 'muted'
    | 'info'
    | 'success'
    | 'warning'
    | 'danger';
  export type ConstellationDataTableContent =
    | Snippet
    | string
    | number
    | boolean
    | null
    | undefined;

  export type ConstellationDataTableColumn = {
    key?: string;
    label: string;
    align?: ConstellationDataTableAlign;
    width?: string;
    minWidth?: string;
    nowrap?: boolean;
    className?: string;
  };

  export type ConstellationDataTableCell = {
    content?: ConstellationDataTableContent;
    align?: ConstellationDataTableAlign;
    tone?: ConstellationDataTableCellTone;
    strong?: boolean;
    nowrap?: boolean;
    className?: string;
    title?: string;
    colspan?: number;
    rowHeader?: boolean;
  };

  export type ConstellationDataTableRow = {
    id?: string | number;
    cells: ReadonlyArray<ConstellationDataTableCell | ConstellationDataTableContent>;
    tone?: ConstellationDataTableTone;
    interactive?: boolean;
    className?: string;
  };

  type NormalizedColumn = {
    key: string;
    label: string;
    align: ConstellationDataTableAlign;
    width: string;
    minWidth: string;
    nowrap: boolean;
    className: string;
  };

  type NormalizedCell = {
    content?: ConstellationDataTableContent;
    align: ConstellationDataTableAlign;
    tone: ConstellationDataTableCellTone;
    strong: boolean;
    nowrap: boolean;
    className: string;
    title: string;
    colspan?: number;
    rowHeader: boolean;
  };

  type NormalizedRow = {
    id: string;
    cells: NormalizedCell[];
    tone: ConstellationDataTableTone;
    interactive: boolean;
    className: string;
  };

  type Props = {
    columns?: ReadonlyArray<ConstellationDataTableColumn | string>;
    rows?: ReadonlyArray<
      ConstellationDataTableRow | ReadonlyArray<ConstellationDataTableCell | ConstellationDataTableContent>
    >;
    caption?: string;
    description?: string;
    tableCaption?: string;
    density?: ConstellationDataTableDensity;
    compact?: boolean;
    empty?: boolean;
    emptyTitle?: string;
    emptyDescription?: string;
    emptyLabel?: string;
    className?: string;
    style?: string;
    toolbar?: Snippet;
    footer?: Snippet;
    children?: Snippet;
  };

  let {
    columns = [],
    rows = [],
    caption = '',
    description = '',
    tableCaption = '',
    density = 'comfortable',
    compact = false,
    empty = false,
    emptyTitle = 'Nothing to show yet',
    emptyDescription = '',
    emptyLabel = 'No rows to display.',
    className = '',
    style = '',
    toolbar,
    footer,
    children,
  }: Props = $props();

  function isSnippet(value: ConstellationDataTableContent): value is Snippet {
    return typeof value === 'function';
  }

  function isColumn(
    value: ConstellationDataTableColumn | string,
  ): value is ConstellationDataTableColumn {
    return typeof value === 'object' && value !== null;
  }

  function isCell(
    value: ConstellationDataTableCell | ConstellationDataTableContent,
  ): value is ConstellationDataTableCell {
    return (
      typeof value === 'object' &&
      value !== null &&
      !isSnippet(value as ConstellationDataTableContent) &&
      ('content' in value ||
        'align' in value ||
        'tone' in value ||
        'strong' in value ||
        'nowrap' in value ||
        'className' in value ||
        'title' in value ||
        'colspan' in value ||
        'rowHeader' in value)
    );
  }

  function isRow(
    value:
      | ConstellationDataTableRow
      | ReadonlyArray<ConstellationDataTableCell | ConstellationDataTableContent>,
  ): value is ConstellationDataTableRow {
    return !Array.isArray(value) && typeof value === 'object' && value !== null && 'cells' in value;
  }

  function normalizeColumn(
    column: ConstellationDataTableColumn | string,
    index: number,
  ): NormalizedColumn {
    if (!isColumn(column)) {
      return {
        key: `column-${index + 1}`,
        label: column,
        align: 'start',
        width: '',
        minWidth: '',
        nowrap: false,
        className: '',
      };
    }

    return {
      key: column.key ?? `${column.label}-${index + 1}`,
      label: column.label,
      align: column.align ?? 'start',
      width: column.width ?? '',
      minWidth: column.minWidth ?? '',
      nowrap: column.nowrap ?? false,
      className: column.className ?? '',
    };
  }

  function normalizeCell(
    cell: ConstellationDataTableCell | ConstellationDataTableContent,
    column: NormalizedColumn | undefined,
  ): NormalizedCell {
    if (!isCell(cell)) {
      return {
        content: cell,
        align: column?.align ?? 'start',
        tone: 'default',
        strong: false,
        nowrap: column?.nowrap ?? false,
        className: '',
        title: '',
        colspan: undefined,
        rowHeader: false,
      };
    }

    return {
      content: cell.content,
      align: cell.align ?? column?.align ?? 'start',
      tone: cell.tone ?? 'default',
      strong: cell.strong ?? false,
      nowrap: cell.nowrap ?? column?.nowrap ?? false,
      className: cell.className ?? '',
      title: cell.title ?? '',
      colspan: cell.colspan,
      rowHeader: cell.rowHeader ?? false,
    };
  }

  function normalizeRow(
    row:
      | ConstellationDataTableRow
      | ReadonlyArray<ConstellationDataTableCell | ConstellationDataTableContent>,
    rowIndex: number,
    sourceColumns: NormalizedColumn[],
  ): NormalizedRow {
    if (!isRow(row)) {
      return {
        id: `row-${rowIndex + 1}`,
        cells: row.map((cell, cellIndex) => normalizeCell(cell, sourceColumns[cellIndex])),
        tone: 'default',
        interactive: false,
        className: '',
      };
    }

    return {
      id: String(row.id ?? `row-${rowIndex + 1}`),
      cells: row.cells.map((cell, cellIndex) => normalizeCell(cell, sourceColumns[cellIndex])),
      tone: row.tone ?? 'default',
      interactive: row.interactive ?? false,
      className: row.className ?? '',
    };
  }

  function columnStyle(column: NormalizedColumn): string {
    const parts: string[] = [];
    if (column.width) parts.push(`width: ${column.width}`);
    if (column.minWidth) parts.push(`min-width: ${column.minWidth}`);
    return parts.join('; ');
  }

  const resolvedDensity = $derived(compact ? 'compact' : density);
  const normalizedColumns = $derived(columns.map((column, index) => normalizeColumn(column, index)));
  const normalizedRows = $derived(rows.map((row, rowIndex) => normalizeRow(row, rowIndex, normalizedColumns)));
  const columnCount = $derived(
    Math.max(
      normalizedColumns.length,
      normalizedRows.reduce((max, row) => Math.max(max, row.cells.length), 0),
      1,
    ),
  );
  const hasRows = $derived(normalizedRows.length > 0);
  const hasCustomTable = $derived(Boolean(children));
  const showHeader = $derived(Boolean(caption || description || toolbar));
  const rootClass = $derived(
    ['constellation-data-table', `constellation-data-table-${resolvedDensity}`, className]
      .filter(Boolean)
      .join(' '),
  );
</script>

<section class={rootClass} {style}>
  {#if showHeader}
    <div class="constellation-data-table-header">
      <div class="constellation-data-table-copy">
        {#if caption}
          <h3 class="constellation-data-table-caption">{caption}</h3>
        {/if}

        {#if description}
          <p class="constellation-data-table-description">{description}</p>
        {/if}
      </div>

      {#if toolbar}
        <div class="constellation-data-table-toolbar">
          {@render toolbar()}
        </div>
      {/if}
    </div>
  {/if}

  {#if empty}
    <div class="constellation-data-table-empty-state">
      <ConstellationEmptyState size="sm" title={emptyTitle} description={emptyDescription} />
    </div>
  {:else}
    <div class="constellation-data-table-scroll">
      <table class="constellation-data-table-table">
        {#if tableCaption}
          <caption class="constellation-data-table-table-caption">{tableCaption}</caption>
        {/if}

        {#if hasCustomTable}
          {@render children?.()}
        {:else}
          {#if normalizedColumns.length > 0}
            <thead>
              <tr>
                {#each normalizedColumns as column (column.key)}
                  <th
                    class={`constellation-data-table-column constellation-data-table-align-${column.align} ${column.nowrap ? 'is-nowrap' : ''} ${column.className}`.trim()}
                    scope="col"
                    style={columnStyle(column)}
                  >
                    {column.label}
                  </th>
                {/each}
              </tr>
            </thead>
          {/if}

          <tbody>
            {#if hasRows}
              {#each normalizedRows as row (row.id)}
                <tr
                  class={`constellation-data-table-row constellation-data-table-row-tone-${row.tone} ${row.interactive ? 'is-interactive' : ''} ${row.className}`.trim()}
                >
                  {#each row.cells as cell, cellIndex (`${row.id}-${cellIndex + 1}`)}
                    {@const cellClass = `constellation-data-table-cell constellation-data-table-align-${cell.align} constellation-data-table-cell-tone-${cell.tone} ${cell.strong ? 'is-strong' : ''} ${cell.nowrap ? 'is-nowrap' : ''} ${cell.className}`.trim()}
                    {#if cell.rowHeader}
                      <th
                        class={cellClass}
                        scope="row"
                        colspan={cell.colspan}
                        title={cell.title || undefined}
                      >
                        {#if isSnippet(cell.content)}
                          {@render cell.content()}
                        {:else if cell.content != null}
                          {cell.content}
                        {/if}
                      </th>
                    {:else}
                      <td class={cellClass} colspan={cell.colspan} title={cell.title || undefined}>
                        {#if isSnippet(cell.content)}
                          {@render cell.content()}
                        {:else if cell.content != null}
                          {cell.content}
                        {/if}
                      </td>
                    {/if}
                  {/each}
                </tr>
              {/each}
            {:else}
              <tr class="constellation-data-table-empty-row">
                <td class="constellation-data-table-empty" colspan={columnCount}>
                  {emptyLabel}
                </td>
              </tr>
            {/if}
          </tbody>
        {/if}
      </table>
    </div>
  {/if}

  {#if footer}
    <div class="constellation-data-table-footer">
      {@render footer()}
    </div>
  {/if}
</section>

<style>
  .constellation-data-table {
    display: grid;
    gap: 14px;
    min-width: 0;
  }

  .constellation-data-table-header,
  .constellation-data-table-copy,
  .constellation-data-table-toolbar,
  .constellation-data-table-footer {
    display: flex;
    gap: 12px;
    min-width: 0;
  }

  .constellation-data-table-header {
    align-items: flex-end;
    justify-content: space-between;
    flex-wrap: wrap;
  }

  .constellation-data-table-copy {
    flex-direction: column;
  }

  .constellation-data-table-caption {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-size: 15px;
    font-weight: 560;
    line-height: 1.35;
    letter-spacing: 0;
  }

  .constellation-data-table-description,
  .constellation-data-table-table-caption {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.55;
    text-align: left;
  }

  .constellation-data-table-table-caption {
    padding: 0 0 12px;
    caption-side: top;
  }

  .constellation-data-table-toolbar {
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
  }

  .constellation-data-table-scroll {
    overflow-x: auto;
    border-radius: calc(var(--constellation-radius-panel) - 4px);
    border: 1px solid var(--constellation-data-table-scroll-border);
    background: var(--constellation-data-table-scroll-background);
    box-shadow: var(--constellation-data-table-scroll-shadow);
    scrollbar-width: thin;
    scrollbar-color: var(--constellation-data-table-scrollbar) transparent;
  }

  .constellation-data-table-table {
    width: 100%;
    min-width: max(100%, 560px);
    border-collapse: collapse;
  }

  .constellation-data-table-column,
  .constellation-data-table-cell {
    padding: 12px 14px;
    border-bottom: 1px solid var(--constellation-data-table-cell-border);
    vertical-align: top;
  }

  .constellation-data-table-column {
    color: var(--constellation-data-table-column);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    line-height: 1.4;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .constellation-data-table-cell {
    color: var(--constellation-data-table-cell);
    font-size: 13px;
    line-height: 1.55;
    font-weight: 430;
  }

  .constellation-data-table-row {
    --table-row-background: transparent;
  }

  .constellation-data-table-row .constellation-data-table-cell,
  .constellation-data-table-row th.constellation-data-table-cell {
    background:
      linear-gradient(90deg, var(--table-row-background), transparent 68%),
      var(--constellation-data-table-row-sheen);
    transition: background-color var(--constellation-motion-settle-duration) ease;
  }

  .constellation-data-table-row.is-interactive:hover {
    --table-row-background: var(--constellation-data-table-row-hover);
  }

  .constellation-data-table-row-tone-default {
    --table-row-background: transparent;
  }

  .constellation-data-table-row-tone-info {
    --table-row-background: var(--constellation-data-table-row-info);
  }

  .constellation-data-table-row-tone-success {
    --table-row-background: var(--constellation-data-table-row-success);
  }

  .constellation-data-table-row-tone-warning {
    --table-row-background: var(--constellation-data-table-row-warning);
  }

  .constellation-data-table-row-tone-danger {
    --table-row-background: var(--constellation-data-table-row-danger);
  }

  .constellation-data-table-cell.is-strong,
  tbody th.constellation-data-table-cell {
    color: var(--constellation-color-text-primary);
    font-weight: 560;
  }

  .constellation-data-table-cell-tone-default {
    color: var(--constellation-data-table-cell);
  }

  .constellation-data-table-cell-tone-muted {
    color: var(--constellation-data-table-cell-muted);
  }

  .constellation-data-table-cell-tone-info {
    color: var(--constellation-data-table-cell-info);
  }

  .constellation-data-table-cell-tone-success {
    color: var(--constellation-data-table-cell-success);
  }

  .constellation-data-table-cell-tone-warning {
    color: var(--constellation-data-table-cell-warning);
  }

  .constellation-data-table-cell-tone-danger {
    color: var(--constellation-data-table-cell-danger);
  }

  .constellation-data-table-align-start {
    text-align: left;
  }

  .constellation-data-table-align-center {
    text-align: center;
  }

  .constellation-data-table-align-end {
    text-align: right;
  }

  .constellation-data-table .is-nowrap {
    white-space: nowrap;
  }

  .constellation-data-table-empty-row td {
    border-bottom: 0;
  }

  .constellation-data-table-empty {
    padding: 20px 14px;
    color: var(--constellation-data-table-empty);
    font-size: 12px;
    line-height: 1.55;
    text-align: center;
  }

  .constellation-data-table-empty-state {
    min-width: 0;
  }

  .constellation-data-table-footer {
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    color: var(--constellation-color-text-tertiary);
    font-size: 12px;
    line-height: 1.5;
  }

  .constellation-data-table-compact .constellation-data-table-column,
  .constellation-data-table-compact .constellation-data-table-cell {
    padding: 10px 12px;
  }

  @media (max-width: 720px) {
    .constellation-data-table-table {
      min-width: 560px;
    }
  }
</style>
