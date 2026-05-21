import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildProjectDraftPanelView,
  publishOperationPath,
  resourceMeta,
  resourceTitle,
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
