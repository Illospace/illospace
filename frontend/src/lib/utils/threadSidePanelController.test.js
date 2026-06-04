import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildThreadSidePanelAddMenuItems,
  createDefaultThreadSidePanelTabs,
  filePreviewThreadSidePanelTabId,
  isThreadSidePanelSingletonKind,
  openFilePreviewThreadSidePanelTab,
  openSingletonThreadSidePanelTab,
  THREAD_SIDE_PANEL_DEFAULT_TAB_KINDS,
  THREAD_SIDE_PANEL_SINGLETON_TAB_DEFINITIONS,
  threadSidePanelIconForKind,
} from '../features/threads/controllers/threadSidePanelController.ts';

test('default thread side panel opens Discussion, Activity, Handoff, and Project', () => {
  const tabs = createDefaultThreadSidePanelTabs();

  assert.deepEqual(tabs.map((tab) => tab.kind), [...THREAD_SIDE_PANEL_DEFAULT_TAB_KINDS]);
  assert.deepEqual(tabs.map((tab) => tab.kind), ['discussion', 'activity', 'handoff-summary', 'project']);
  assert.deepEqual(tabs.map((tab) => tab.label), ['Discussion', 'Activity', 'Handoff', 'Project']);
});

test('singleton tab definitions drive add menu labels and icons', () => {
  const tabs = createDefaultThreadSidePanelTabs();
  const menuItems = buildThreadSidePanelAddMenuItems(tabs, []);
  const menuKinds = menuItems.map((item) => item.kind);

  assert.deepEqual(menuKinds.slice(0, 4), ['browser', 'vault', 'cycles', 'code-review']);
  for (const definition of THREAD_SIDE_PANEL_SINGLETON_TAB_DEFINITIONS) {
    assert.equal(isThreadSidePanelSingletonKind(definition.kind), true);
    assert.equal(threadSidePanelIconForKind(definition.kind), definition.icon);
  }
});

test('closed singleton tabs are restored from the side panel add menu', () => {
  const tabs = [{ id: 'activity', label: 'Activity', kind: 'activity', closeable: true }];

  const menuItems = buildThreadSidePanelAddMenuItems(tabs, []);
  assert.ok(menuItems.some((item) => item.kind === 'handoff-summary' && item.label === 'Handoff'));
  assert.ok(menuItems.some((item) => item.kind === 'project' && item.label === 'Project'));

  const next = openSingletonThreadSidePanelTab(
    { tabs, activeTabId: 'activity', nextBrowserTabIndex: 1 },
    'handoff-summary',
  );

  assert.equal(next.activeTabId, 'handoff-summary');
  assert.ok(next.tabs.some((tab) => tab.kind === 'handoff-summary' && tab.label === 'Handoff'));
});

test('file preview tabs are dynamic and reused by path', () => {
  const state = {
    tabs: createDefaultThreadSidePanelTabs(),
    activeTabId: 'activity',
    nextBrowserTabIndex: 1,
  };

  const first = openFilePreviewThreadSidePanelTab(state, 'docs/diagrams/current-generation-architecture.puml', 101);
  const second = openFilePreviewThreadSidePanelTab(first, 'docs/GENERATION_DISPATCHER_PRD.md');
  const reused = openFilePreviewThreadSidePanelTab(second, 'docs/diagrams/current-generation-architecture.puml', 202);

  assert.equal(first.activeTabId, filePreviewThreadSidePanelTabId('docs/diagrams/current-generation-architecture.puml'));
  assert.equal(first.tabs.at(-1).kind, 'file-preview');
  assert.equal(first.tabs.at(-1).label, 'current-generation-architecture.puml');
  assert.equal(first.tabs.at(-1).filePath, 'docs/diagrams/current-generation-architecture.puml');
  assert.equal(first.tabs.at(-1).runId, 101);
  assert.equal(second.tabs.filter((tab) => tab.kind === 'file-preview').length, 2);
  assert.equal(reused.tabs.filter((tab) => tab.kind === 'file-preview').length, 2);
  assert.equal(reused.activeTabId, first.activeTabId);
  assert.equal(
    reused.tabs.find((tab) => tab.id === first.activeTabId).runId,
    202,
  );
});
