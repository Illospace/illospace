import test from 'node:test';
import assert from 'node:assert/strict';
import * as controller from '../features/cortex/controllers/runSettingsController.ts';

class MemoryStorage {
  constructor(entries = {}) {
    this.entries = new Map(Object.entries(entries));
  }

  getItem(key) {
    return this.entries.has(key) ? this.entries.get(key) : null;
  }

  setItem(key, value) {
    this.entries.set(key, String(value));
  }

  removeItem(key) {
    this.entries.delete(key);
  }
}

test('workspace-default composer state omits model and effort routing metadata', () => {
  const options = controller.normalizeRunOptions(
    {},
    controller.DEFAULT_CORTEX_RUN_SETTINGS,
  );

  assert.equal(Object.hasOwn(options, 'model'), false);
  assert.equal(Object.hasOwn(options, 'effortLevel'), false);
  assert.deepEqual(controller.routingMetadataForRunOptions(options), {});
});

test('explicit composer picks emit model and effort routing metadata', () => {
  const options = controller.normalizeRunOptions(
    {
      model: 'openai/gpt-5.6-sol',
      effortLevel: 'xhigh',
    },
    controller.DEFAULT_CORTEX_RUN_SETTINGS,
  );

  assert.deepEqual(controller.routingMetadataForRunOptions(options), {
    model: 'openai/gpt-5.6-sol',
    thinking_tier: 'xhigh',
    effort: 'xhigh',
  });
});

test('legacy stored picks migrate once, then new explicit picks persist', () => {
  const keys = controller.CORTEX_RUN_SETTINGS_STORAGE_KEYS;
  const storage = new MemoryStorage({
    'illo:cortex:execution-profile': 'deep',
    [keys.model]: 'openai/gpt-5.6-sol',
    [keys.effortLevel]: 'xhigh',
  });

  assert.deepEqual(
    controller.loadRunSettings(storage),
    controller.DEFAULT_CORTEX_RUN_SETTINGS,
  );
  assert.equal(storage.getItem('illo:cortex:execution-profile'), null);
  assert.equal(storage.getItem(keys.model), null);
  assert.equal(storage.getItem(keys.effortLevel), null);

  assert.equal(
    controller.persistRunSettings(storage, {
      model: 'openai/gpt-5.5',
      effortLevel: 'high',
    }),
    true,
  );
  assert.deepEqual(controller.loadRunSettings(storage), {
    model: 'openai/gpt-5.5',
    effortLevel: 'high',
  });
});
