import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildThreadSidePanelAddMenuItems,
  createDefaultThreadSidePanelTabs,
  isThreadSidePanelSingletonKind,
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
