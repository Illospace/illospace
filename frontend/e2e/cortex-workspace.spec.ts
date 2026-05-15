import { expect, test } from '@playwright/test';
import { expectNoUnhandledApiRequests, mockProductApi, sampleIdea } from './fixtures';

test('cortex workspace boots from the bootstrap contract and exposes the prompt surface', async ({ page }) => {
  const api = await mockProductApi(page, [
    {
      path: '/api/cortex/bootstrap',
      response: {
        ideas: [sampleIdea],
        connections: [],
        team_members: [],
        workspace_apps: [],
        workspace_pins: [],
        selected_idea: null,
        direct_thread: null,
        auth_status: { authenticated: true },
        meta: { contract: 'workspace' },
      },
    },
  ]);

  await page.goto('/cortex');

  await expect(page.getByLabel('Workspace prompt')).toBeVisible();
  await expect(page.getByLabel('Workspace chat')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Notifications' })).toBeVisible();
  await expectNoUnhandledApiRequests(api);
});
