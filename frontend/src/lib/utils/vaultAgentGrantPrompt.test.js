import test from 'node:test';
import assert from 'node:assert/strict';

import {
  normalizeVaultAgentGrantPromptMessage,
  vaultAgentGrantPromptFromRunToolEvent,
  vaultAgentGrantPromptFromStream,
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

test('does not recover grant prompts from redacted durable brain_vault run events', () => {
  const prompt = vaultAgentGrantPromptFromRunToolEvent({
    type: 'tool_finished',
    tool_name: 'brain_vault',
    idea_id: 'idea-1',
    run_id: 199,
    event_created_at: '2026-05-19T14:00:00Z',
    result: '[secret redacted]',
  });

  assert.equal(prompt, null);
});

test('requires matching target user when recovering grant prompts from run events', () => {
  const event = {
    type: 'tool_finished',
    tool_name: 'brain_vault',
    idea_id: 'idea-1',
    run_id: 199,
    result: JSON.stringify({
      error: 'Vault grant required before this agent can read the secret',
      status: 'pending',
      grant_id: 7,
      key_name: 'GITHUB_TOKEN',
      target_user_id: 'user-1',
    }),
  };

  assert.equal(vaultAgentGrantPromptFromRunToolEvent(event, 'user-1')?.grant_id, 7);
  assert.equal(vaultAgentGrantPromptFromRunToolEvent(event, 'user-2'), null);
  assert.equal(
    vaultAgentGrantPromptFromRunToolEvent(
      {
        ...event,
        result: JSON.stringify({
          error: 'Vault grant required before this agent can read the secret',
          status: 'pending',
          grant_id: 7,
          key_name: 'GITHUB_TOKEN',
        }),
      },
      'user-1',
    ),
    null,
  );
});

test('ignores redacted brain_vault results in loaded run streams', () => {
  const stream = [
    {
      type: 'run',
      id: 199,
      idea_id: 'idea-1',
      tool_calls: [
        {
          tool_name: 'brain_vault',
          finished_at: '2026-05-19T14:00:00Z',
          result: '[secret redacted]',
        },
      ],
    },
  ];

  const prompt = vaultAgentGrantPromptFromStream(stream, 'idea-1', new Set(), 'user-1');
  assert.equal(vaultAgentGrantPromptFromStream(stream, 'idea-1', new Set(), 'user-2'), null);
  assert.equal(prompt, null);
});
