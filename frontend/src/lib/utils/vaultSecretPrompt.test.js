import test from 'node:test';
import assert from 'node:assert/strict';

import {
  normalizeVaultSecretPromptMessage,
  vaultSecretPromptFromRunToolEvent,
  vaultSecretPromptFromStream,
} from './vaultSecretPrompt.ts';

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

test('extracts vault prompts from completed tool events', () => {
  const prompt = vaultSecretPromptFromRunToolEvent({
    type: 'tool_finished',
    tool_name: 'vault_secret_prompt',
    status: 'completed',
    idea_id: 'idea-1',
    run_id: 42,
    event_created_at: '2026-05-13T15:27:22Z',
    result: JSON.stringify({
      key_name: 'ILLO_TEST_API_KEY',
      description: 'Temporary test secret',
      category: 'api',
      status: 'opened',
    }),
  });

  assert.equal(prompt?.id, 'vault-secret-42-ILLO_TEST_API_KEY');
  assert.equal(prompt?.idea_id, 'idea-1');
  assert.equal(prompt?.key_name, 'ILLO_TEST_API_KEY');
  assert.equal(prompt?.created_at, '2026-05-13T15:27:22Z');
});

test('uses stable prompt id from tool result when present', () => {
  const prompt = vaultSecretPromptFromRunToolEvent({
    type: 'tool_finished',
    tool_name: 'vault_secret_prompt',
    status: 'completed',
    idea_id: 'idea-1',
    run_id: 42,
    result: JSON.stringify({
      prompt: {
        id: 'prompt-stable',
        key_name: 'ANTHROPIC_API_KEY',
        category: 'api',
      },
    }),
  });

  assert.equal(prompt?.id, 'prompt-stable');
  assert.equal(prompt?.key_name, 'ANTHROPIC_API_KEY');
});

test('recovers latest vault prompt from persisted run stream', () => {
  const prompt = vaultSecretPromptFromStream(
    [
      {
        type: 'run',
        id: '41',
        run_id: 41,
        idea_id: 'idea-1',
        tool_calls: [
          {
            tool: 'vault_secret_prompt',
            status: 'completed',
            finished_at: '2026-05-13T15:00:00Z',
            result: JSON.stringify({ key_name: 'OLD_KEY', category: 'api' }),
          },
        ],
      },
      {
        type: 'run',
        id: '42',
        run_id: 42,
        idea_id: 'idea-1',
        tool_calls: [
          {
            tool: 'vault_secret_prompt',
            status: 'completed',
            finished_at: '2026-05-13T16:00:00Z',
            result: JSON.stringify({ key_name: 'NEW_KEY', category: 'service' }),
          },
        ],
      },
    ],
    'idea-1',
  );

  assert.equal(prompt?.key_name, 'NEW_KEY');
  assert.equal(prompt?.category, 'service');
});

test('does not recover dismissed vault prompts from persisted stream', () => {
  const prompt = vaultSecretPromptFromStream(
    [
      {
        type: 'run',
        id: '42',
        run_id: 42,
        idea_id: 'idea-1',
        tool_calls: [
          {
            tool: 'vault_secret_prompt',
            status: 'completed',
            result: JSON.stringify({ key_name: 'DISMISSED_KEY', category: 'api' }),
          },
        ],
      },
    ],
    'idea-1',
    new Set(['vault-secret-42-DISMISSED_KEY']),
  );

  assert.equal(prompt, null);
});
