import { expect, test } from '@playwright/test';
import { e2eUser, expectNoUnhandledApiRequests, mockProductApi } from './fixtures';

test('submitting the workspace prompt creates a thread message and queues a run', async ({ page }) => {
  let ideaPayload: Record<string, unknown> | null = null;
  let messagePayload: Record<string, any> | null = null;
  let statusPayload: Record<string, unknown> | null = null;
  let notifyPayload: Record<string, any> | null = null;

  const createdIdea = {
    id: 'idea-run-e2e',
    title: 'Check the deployment diff',
    description: null,
    status: 'new',
    origin: 'user',
    origin_ref: null,
    salience_score: 0.5,
    position_x: 0,
    position_y: 0,
    created_at: '2026-05-01T10:00:00Z',
    updated_at: '2026-05-01T10:00:00Z',
    user_id: e2eUser.id,
    author_name: e2eUser.name,
    author_color: e2eUser.color,
    thread_count: 0,
    active_agents: 0,
    attachments: [],
    metadata: {},
  };

  const api = await mockProductApi(page, [
    {
      method: 'POST',
      path: '/api/cortex/ideas',
      response: async (request) => {
        ideaPayload = await request.postDataJSON();
        return { ...createdIdea, title: String(ideaPayload?.title ?? createdIdea.title) };
      },
    },
    {
      method: 'POST',
      path: '/api/cortex/ideas/idea-run-e2e/thread',
      response: async (request) => {
        messagePayload = await request.postDataJSON();
        return {
          id: 501,
          idea_id: 'idea-run-e2e',
          role: 'user',
          content: messagePayload?.content,
          metadata: messagePayload?.metadata ?? {},
          attachments: messagePayload?.attachments ?? [],
          created_at: '2026-05-01T10:00:01Z',
        };
      },
    },
    {
      method: 'PATCH',
      path: '/api/cortex/ideas/idea-run-e2e/status',
      response: async (request) => {
        statusPayload = await request.postDataJSON();
        return { ...createdIdea, status: statusPayload?.status ?? 'queued' };
      },
    },
    {
      method: 'POST',
      path: '/api/cortex/notify',
      response: async (request) => {
        notifyPayload = await request.postDataJSON();
        return { ok: true, run_id: 7001 };
      },
    },
    { method: 'POST', path: '/api/cortex/generate-title', response: { title: 'Deployment diff' } },
    { method: 'PUT', path: '/api/cortex/ideas/idea-run-e2e', response: { ...createdIdea, display_title: 'Deployment diff' } },
    { path: '/api/cortex/ideas/idea-run-e2e/unified-stream', response: [] },
    { method: 'POST', path: '/api/cortex/ideas/idea-run-e2e/mark-read', response: { ok: true } },
  ]);

  await page.goto('/cortex');
  await page.getByLabel('Workspace prompt').fill('Check the deployment diff');
  await page.keyboard.press('Enter');

  await expect.poll(() => notifyPayload).not.toBeNull();
  expect(ideaPayload).toMatchObject({ title: 'Check the deployment diff' });
  expect(messagePayload).toMatchObject({
    content: 'Check the deployment diff',
    metadata: {
      execution_profile: 'fast',
      model_tier: 'high',
      thinking_tier: 'high',
    },
  });
  expect(statusPayload).toEqual({ status: 'queued' });
  expect(notifyPayload).toMatchObject({
    event: 'idea_created',
    idea_id: 'idea-run-e2e',
    thread_message: 'Check the deployment diff',
  });
  await expectNoUnhandledApiRequests(api);
});
