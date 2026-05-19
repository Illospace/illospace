import test from 'node:test';
import assert from 'node:assert/strict';

import {
  normalizeVaultAgentGrantPromptMessage,
  vaultAgentGrantPromptFromToolResult,
} from './vaultAgentGrantPrompt.ts';

test('normalizes vault grant prompt websocket payloads with nested grant data', () => {
  assert.deepEqual(
    normalizeVaultAgentGrantPromptMessage({
      type: 'vault_agent_grant_prompt',
      idea_id: 'idea-1',
      prompt: {
        id: 'grant-prompt-7',
        grant_id: 7,
        key_name: 'GITHUB_TOKEN',
        reason: 'List private repos for this run.',
      },
      grant: {
        id: 7,
        run_id: 199,
        requested_by: 'illo',
        requested_at: '2026-05-19T14:00:00Z',
      },
    }),
    {
      id: 'grant-prompt-7',
      idea_id: 'idea-1',
      grant_id: 7,
      key_name: 'GITHUB_TOKEN',
      run_id: 199,
      reason: 'List private repos for this run.',
      requested_by: 'illo',
      requested_at: '2026-05-19T14:00:00Z',
      created_at: null,
    },
  );
});

test('rejects incomplete vault grant prompt events', () => {
  assert.equal(normalizeVaultAgentGrantPromptMessage({ idea_id: 'idea-1', key_name: 'GITHUB_TOKEN' }), null);
  assert.equal(normalizeVaultAgentGrantPromptMessage({ grant_id: 7, key_name: 'GITHUB_TOKEN' }), null);
});

test('can recover a grant prompt from a pending brain_vault result when metadata is present', () => {
  const prompt = vaultAgentGrantPromptFromToolResult(
    JSON.stringify({
      error: 'Vault grant required before this agent can read the secret',
      status: 'pending',
      grant_id: 7,
      key_name: 'GITHUB_TOKEN',
    }),
    {
      ideaId: 'idea-1',
      runId: 199,
      createdAt: '2026-05-19T14:00:00Z',
    },
  );

  assert.equal(prompt?.id, 'vault-grant-idea-1-7');
  assert.equal(prompt?.idea_id, 'idea-1');
  assert.equal(prompt?.grant_id, 7);
  assert.equal(prompt?.key_name, 'GITHUB_TOKEN');
  assert.equal(prompt?.run_id, 199);
});

