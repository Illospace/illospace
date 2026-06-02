import test from 'node:test';
import assert from 'node:assert/strict';

import { signalBlobPointerUpAction } from '../components/constellation/signalBlobActivation.ts';

test('activates signal blobs on pointer-up when the pointer did not drag', () => {
  assert.equal(
    signalBlobPointerUpAction({
      dragMoved: false,
      pointerMoved: false,
      canActivate: true,
    }),
    'activate',
  );
});

test('suppresses signal blob click after drag movement', () => {
  assert.equal(
    signalBlobPointerUpAction({
      dragMoved: true,
      pointerMoved: false,
      canActivate: true,
    }),
    'suppress-click',
  );
});

test('suppresses signal blob click after component pointer movement', () => {
  assert.equal(
    signalBlobPointerUpAction({
      dragMoved: false,
      pointerMoved: true,
      canActivate: true,
    }),
    'suppress-click',
  );
});

test('does nothing when a non-drag pointer-up has no activation callback', () => {
  assert.equal(
    signalBlobPointerUpAction({
      dragMoved: false,
      pointerMoved: false,
      canActivate: false,
    }),
    'none',
  );
});
