import test from 'node:test';
import assert from 'node:assert/strict';

import {
  edgeOptionIndex,
  enabledOptionIndexes,
  initialOptionIndex,
  nextOptionIndex,
} from './selectKeyboard.ts';

const options = [
  { value: 'alpha' },
  { value: 'bravo', disabled: true },
  { value: 'charlie' },
];

test('option navigation wraps past both ends', () => {
  assert.equal(nextOptionIndex(options, 2, 1), 0);
  assert.equal(nextOptionIndex(options, 0, -1), 2);
});

test('option navigation skips disabled options', () => {
  assert.deepEqual(enabledOptionIndexes(options), [0, 2]);
  assert.equal(nextOptionIndex(options, 0, 1), 2);
  assert.equal(nextOptionIndex(options, 2, -1), 0);
});

test('an all-disabled option list has no keyboard target', () => {
  const disabledOptions = [
    { value: 'alpha', disabled: true },
    { value: 'bravo', disabled: true },
  ];

  assert.deepEqual(enabledOptionIndexes(disabledOptions), []);
  assert.equal(initialOptionIndex(disabledOptions, 'alpha'), -1);
  assert.equal(nextOptionIndex(disabledOptions, 0, 1), -1);
  assert.equal(edgeOptionIndex(disabledOptions, 'first'), -1);
  assert.equal(edgeOptionIndex(disabledOptions, 'last'), -1);
});

test('an empty option list has no keyboard target', () => {
  assert.deepEqual(enabledOptionIndexes([]), []);
  assert.equal(initialOptionIndex([], 'missing'), -1);
  assert.equal(nextOptionIndex([], -1, 1), -1);
  assert.equal(edgeOptionIndex([], 'first'), -1);
  assert.equal(edgeOptionIndex([], 'last'), -1);
});

test('initial navigation uses the selected option or the first enabled fallback', () => {
  assert.equal(initialOptionIndex(options, 'charlie'), 2);
  assert.equal(initialOptionIndex(options, 'missing'), 0);
});

test('Home and End navigation targets the first and last enabled options', () => {
  assert.equal(edgeOptionIndex(options, 'first'), 0);
  assert.equal(edgeOptionIndex(options, 'last'), 2);
});
