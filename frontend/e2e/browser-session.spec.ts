import { expect, test } from '@playwright/test';
import { expectNoUnhandledApiRequests, mockProductApi, sampleIdea } from './fixtures';

test('direct thread hydrates an existing browser session into the side panel', async ({ page }) => {
  let browserSessionRequested = false;
  const api = await mockProductApi(page, [
    {
      path: '/api/cortex/bootstrap',
      response: (request) => {
        const url = new URL(request.url());
        if (url.searchParams.get('idea_id') === sampleIdea.id) {
          return {
            ideas: [sampleIdea],
            connections: [],
            team_members: [],
            workspace_apps: [],
            workspace_pins: [],
            selected_idea: sampleIdea,
            direct_thread: {
              idea_id: sampleIdea.id,
              stream: [
                {
                  type: 'message',
                  id: 'message-browser-1',
                  role: 'user',
                  content: 'Open the deployment dashboard.',
                  timestamp: '2026-05-01T10:00:00Z',
                },
              ],
            },
            auth_status: { authenticated: true },
            meta: { contract: 'direct-thread' },
          };
        }
        return {
          ideas: [sampleIdea],
          connections: [],
          team_members: [],
          workspace_apps: [],
          workspace_pins: [],
          selected_idea: null,
          direct_thread: null,
          auth_status: { authenticated: true },
          meta: { contract: 'workspace' },
        };
      },
    },
    {
      path: `/api/cortex/ideas/${sampleIdea.id}/browser/session`,
      response: () => {
        browserSessionRequested = true;
        return {
          id: 'browser-session-e2e',
          idea_id: sampleIdea.id,
          run_id: 7001,
          status: 'running',
          current_url: 'https://example.test/dashboard',
          page_title: 'Deployment Dashboard',
          viewport_width: 1280,
          viewport_height: 800,
          storage_mode: 'ephemeral',
          allow_downloads: false,
          allow_file_uploads: true,
          last_error: null,
          watchers: 1,
          tabs: [{ index: 0, url: 'https://example.test/dashboard', title: 'Deployment Dashboard', active: true }],
          current_tab_index: 0,
          actions: [],
          downloads: [],
          artifacts: [],
          console_messages: [],
          request_failures: [],
        };
      },
    },
    { method: 'POST', path: `/api/cortex/ideas/${sampleIdea.id}/mark-read`, response: { ok: true } },
  ]);

  await page.goto(`/cortex?idea=${sampleIdea.id}`);

  await expect.poll(() => browserSessionRequested).toBe(true);
  await expect(page.getByLabel('Browser')).toBeVisible({ timeout: 6_000 });
  await expect(page.getByText('Deployment Dashboard')).toBeVisible();
  await expectNoUnhandledApiRequests(api);
});
