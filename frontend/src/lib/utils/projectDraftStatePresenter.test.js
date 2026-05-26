import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildProjectDraftPanelView,
  buildProjectFilePreviewView,
  buildProjectTextDiff,
  joinProjectDisplayPath,
  normaliseProjectPreviewText,
  projectFileLayerLabel,
  projectFileKind,
  projectFileKindLabel,
  projectFileStatusTone,
  projectSpreadsheetPreviewRows,
  publishOperationPath,
  resourceMeta,
  resourceTitle,
  visibleProjectExplorerRows,
} from '../features/threads/domain/projectDraftStatePresenter.ts';

test('project draft presenter summarizes root, draft, conflicts, and stale paths', () => {
  const draftState = {
    ok: true,
    run_id: 77,
    draft_status: {
      ok: true,
      resources: [
        {
          id: 'reports',
          mount_path: '/reports',
          kind: 'folder',
          provider: 'local',
          change_source: 'thread draft',
          changes: {
            changed_paths: ['brief.md'],
            new_paths: [{ path: 'appendix.md' }],
            deleted_paths: [],
            conflicted_paths: [{ relative_path: 'summary.md' }],
            out_of_date_paths: ['root.md'],
          },
          file_browser: {
            entries: [
              { path: 'brief.md', name: 'brief.md', status: 'changed', has_root: true, has_draft: true, size: 120 },
              { path: 'appendix.md', name: 'appendix.md', status: 'new', has_root: false, has_draft: true, size: 80 },
              { path: 'archive/old.md', name: 'old.md', status: 'clean', has_root: true, has_draft: false, size: 60 },
            ],
          },
        },
      ],
    },
  };

  const view = buildProjectDraftPanelView({
    draftState,
    loading: false,
    loadError: '',
    runId: null,
  });

  assert.equal(view.runLabel, 'Run 77');
  assert.equal(view.readiness.tone, 'conflict');
  assert.equal(view.aggregateCounts.changed_paths, 1);
  assert.equal(view.aggregateCounts.new_paths, 1);
  assert.equal(view.aggregateCounts.conflicted_paths, 1);
  assert.deepEqual(view.outOfDatePaths, ['/reports/root.md']);
  assert.equal(view.publishPlan.blockedCount, 1);
  assert.equal(view.publishPlan.operationCount, 3);
  assert.equal(view.fileGroups.find((group) => group.key === 'conflicted_paths')?.paths[0], '/reports/summary.md');
  assert.equal(view.fileBrowser.fileCount, 3);
  assert.equal(view.fileBrowser.changedCount, 2);
  assert.ok(view.fileBrowser.files.some((file) => file.displayPath === '/reports/archive/old.md'));
  assert.ok(view.fileBrowser.rows.some((row) => row.kind === 'directory' && row.displayPath === '/reports/archive'));
  assert.equal(resourceTitle(view.resources[0]), '/reports');
  assert.equal(resourceMeta(view.resources[0]), 'folder / local / thread draft');
});

test('project draft presenter respects explicit publish plan summaries', () => {
  const draftState = {
    ok: true,
    draft_status: { ok: true, run_id: 12, resources: [] },
    plan_publish: {
      ok: true,
      plan_only: true,
      mutates_project_root: false,
      summary: {
        resource_count: 1,
        operation_count: 2,
        blocked_count: 0,
      },
      groups: [
        {
          resource_id: 'docs',
          status: 'ready',
          operations: [
            { operation: 'update', target_path: 'strategy/report.md' },
          ],
        },
      ],
    },
  };

  const view = buildProjectDraftPanelView({
    draftState,
    loading: false,
    loadError: '',
    runId: null,
  });

  assert.equal(view.readiness.label, 'Ready');
  assert.equal(view.publishPlan.resourceCount, 1);
  assert.equal(view.publishPlan.operationCount, 2);
  assert.equal(view.publishPlan.readyCount, 1);
  assert.equal(publishOperationPath(view.publishPlan.groups[0].operations?.[0] ?? {}), 'strategy/report.md');
});

test('project file presenter formats status, layer, and mounted paths', () => {
  assert.equal(joinProjectDisplayPath('/reports', 'analysis/summary.md'), '/reports/analysis/summary.md');
  assert.equal(projectFileStatusTone('out_of_date'), 'warning');
  assert.equal(projectFileLayerLabel({ path: 'new.md', has_draft: true, has_root: false }), 'new draft file');
  assert.equal(projectFileKind({ path: 'deck.pdf' }), 'pdf');
  assert.equal(projectFileKind({ path: 'finance.xlsx' }), 'spreadsheet');
  assert.equal(projectFileKind({ path: 'diagram.mmd' }), 'graph');
  assert.equal(projectFileKindLabel({ path: 'analysis.ipynb' }), 'Code');
});

test('project spreadsheet previews parse escaped rows and quoted cells', () => {
  assert.deepEqual(
    projectSpreadsheetPreviewRows('name,notes\\nstripe,"keeps, comma"\\nadyen,plain', '.csv'),
    [
      ['name', 'notes'],
      ['stripe', 'keeps, comma'],
      ['adyen', 'plain'],
    ],
  );

  assert.deepEqual(
    projectSpreadsheetPreviewRows('name\tcount\\nprocessor\t12', '.tsv'),
    [
      ['name', 'count'],
      ['processor', '12'],
    ],
  );
});

test('project file presenter filters rows under collapsed directories', () => {
  const draftState = {
    ok: true,
    draft_status: {
      ok: true,
      resources: [
        {
          id: 'reports',
          mount_path: '/reports',
          file_browser: {
            entries: [
              { path: 'archive/old.md', name: 'old.md', status: 'clean', has_root: true, size: 60 },
              { path: 'archive/new.md', name: 'new.md', status: 'new', has_draft: true, size: 80 },
              { path: 'brief.md', name: 'brief.md', status: 'changed', has_root: true, has_draft: true, size: 120 },
            ],
          },
        },
      ],
    },
  };

  const rows = buildProjectDraftPanelView({
    draftState,
    loading: false,
    loadError: '',
    runId: null,
  }).fileBrowser.rows;
  const archive = rows.find((row) => row.kind === 'directory' && row.path === 'archive');
  assert.ok(archive);

  const visible = visibleProjectExplorerRows(rows, [archive.key]);

  assert.ok(visible.some((row) => row.kind === 'directory' && row.path === 'archive'));
  assert.ok(visible.some((row) => row.kind === 'file' && row.path === 'brief.md'));
  assert.equal(visible.some((row) => row.kind === 'file' && row.path === 'archive/old.md'), false);
});

test('project file preview renders readable root to draft diffs', () => {
  assert.equal(
    normaliseProjectPreviewText('# Title\\n\\nBody'),
    '# Title\n\nBody',
  );

  const lines = buildProjectTextDiff('root\nkeep\n', 'draft\nkeep\nnext\n');
  assert.deepEqual(
    lines.filter((line) => line.kind !== 'context').map((line) => `${line.kind}:${line.text}`),
    ['removed:root', 'added:draft', 'added:next'],
  );

  const view = buildProjectFilePreviewView(
    {
      ok: true,
      path: 'summary.md',
      layers: {
        root: { exists: true, binary: false, size: 20, content: 'root\\nkeep\\n' },
        draft: { exists: true, binary: false, size: 26, content: 'draft\\nkeep\\nnext\\n' },
      },
    },
    {
      kind: 'file',
      key: 'root:summary.md:0',
      resourceId: 'root',
      resourceTitle: 'Project root',
      mountPath: '/',
      path: 'summary.md',
      name: 'summary.md',
      displayPath: '/summary.md',
      depth: 0,
      status: 'changed',
      has_root: true,
      has_draft: true,
    },
  );

  assert.equal(view.mode, 'diff');
  assert.equal(view.canEdit, true);
  assert.equal(view.editableContent, 'draft\nkeep\nnext\n');
  assert.equal(view.finalLayer?.label, 'Final');
  assert.equal(view.finalLayer?.content, 'draft\nkeep\nnext\n');
  assert.ok(view.lines.some((line) => line.kind === 'removed' && line.text === 'root'));
  assert.ok(view.lines.some((line) => line.kind === 'added' && line.text === 'draft'));
});

test('project file preview falls back to layers for binary previews', () => {
  const view = buildProjectFilePreviewView(
    {
      ok: true,
      path: 'image.png',
      layers: {
        root: { exists: true, binary: true, size: 100 },
        draft: { exists: true, binary: true, size: 100 },
      },
    },
    {
      kind: 'file',
      key: 'root:image.png:0',
      resourceId: 'root',
      resourceTitle: 'Project root',
      mountPath: '/',
      path: 'image.png',
      name: 'image.png',
      displayPath: '/image.png',
      depth: 0,
      status: 'clean',
      has_root: true,
      has_draft: true,
    },
  );

  assert.equal(view.mode, 'layers');
  assert.equal(view.canEdit, false);
  assert.equal(view.layers[0].content, 'Binary file preview is not available.');
});
