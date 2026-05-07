import test from 'node:test';
import assert from 'node:assert/strict';

import { createUUID } from './uuid.ts';

test('createUUID falls back when crypto.randomUUID is unavailable', () => {
  const originalCrypto = globalThis.crypto;
  const bytes = new Uint8Array(16);
  for (let i = 0; i < bytes.length; i += 1) {
    bytes[i] = i;
  }

  try {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: {
        getRandomValues(target) {
          target.set(bytes);
          return target;
        },
      },
    });

    const id = createUUID();

    assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.equal(id, '00010203-0405-4607-8809-0a0b0c0d0e0f');
  } finally {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: originalCrypto,
    });
  }
});

test('createUUID prefers crypto.randomUUID when available', () => {
  const originalCrypto = globalThis.crypto;

  try {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: {
        randomUUID() {
          return 'preferred-random-uuid';
        },
        getRandomValues() {
          throw new Error('fallback should not be used');
        },
      },
    });

    assert.equal(createUUID(), 'preferred-random-uuid');
  } finally {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: originalCrypto,
    });
  }
});


test('createUUID still returns a UUID when crypto is unavailable', () => {
  const originalCrypto = globalThis.crypto;

  try {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: undefined,
    });

    const id = createUUID();
    assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  } finally {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: originalCrypto,
    });
  }
});
