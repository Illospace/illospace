import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeVaultSecretPromptMessage } from './vaultSecretPrompt.ts';

test('normalizes vault prompt websocket payloads with nested prompt data', () => {
  assert.deepEqual(
    normalizeVaultSecretPromptMessage({
      type: 'vault_secret_prompt',
      idea_id: 'idea-1',
      org_id: 'org-1',
      prompt: {
        id: 'prompt-1',
        key_name: 'GITHUB_TOKEN',
        description: 'GitHub push token',
        category: 'api',
      },
    }),
    {
      id: 'prompt-1',
      idea_id: 'idea-1',
      key_name: 'GITHUB_TOKEN',
      description: 'GitHub push token',
      category: 'api',
      reason: null,
      requested_by: null,
      created_at: null,
    },
  );
});

test('accepts payload wrappers and non-string thread identifiers', () => {
  assert.deepEqual(
    normalizeVaultSecretPromptMessage({
      type: 'vault_secret_prompt',
      payload: {
        thread_id: 42,
        prompt: {
          keyName: 'SENDGRID_API_KEY',
          requestedBy: 'agent',
        },
      },
    }),
    {
      id: 'vault-secret-42-SENDGRID_API_KEY',
      idea_id: '42',
      key_name: 'SENDGRID_API_KEY',
      description: null,
      category: 'api',
      reason: null,
      requested_by: 'agent',
      created_at: null,
    },
  );
});

test('rejects incomplete vault prompt events', () => {
  assert.equal(normalizeVaultSecretPromptMessage({ idea_id: 'idea-1' }), null);
  assert.equal(normalizeVaultSecretPromptMessage({ prompt: { key_name: 'GITHUB_TOKEN' } }), null);
});
