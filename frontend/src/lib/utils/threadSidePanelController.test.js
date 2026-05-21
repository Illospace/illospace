import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildThreadSidePanelAddMenuItems,
  createDefaultThreadSidePanelTabs,
  openSingletonThreadSidePanelTab,
} from '../features/threads/controllers/threadSidePanelController.ts';

test('default thread side panel opens Discussion beside Activity and Handoff', () => {
  const tabs = createDefaultThreadSidePanelTabs();

  assert.deepEqual(tabs.map((tab) => tab.kind), ['discussion', 'activity', 'handoff-summary']);
  assert.deepEqual(tabs.map((tab) => tab.label), ['Discussion', 'Activity', 'Handoff']);
});

test('handoff summary is restored from the side panel add menu when closed', () => {
  const tabs = [{ id: 'activity', label: 'Activity', kind: 'activity', closeable: true }];

  const menuItems = buildThreadSidePanelAddMenuItems(tabs, []);
  assert.ok(menuItems.some((item) => item.kind === 'handoff-summary' && item.label === 'Handoff'));

  const next = openSingletonThreadSidePanelTab(
    { tabs, activeTabId: 'activity', nextBrowserTabIndex: 1 },
    'handoff-summary',
  );

  assert.equal(next.activeTabId, 'handoff-summary');
  assert.ok(next.tabs.some((tab) => tab.kind === 'handoff-summary' && tab.label === 'Handoff'));
});
