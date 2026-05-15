import { expect, test } from '@playwright/test';
import { e2eUser, expectNoUnhandledApiRequests, mockProductApi } from './fixtures';

test('local workspace app creation sends a generated-app contract through the real cortex shell', async ({ page }) => {
  let createPayload: Record<string, any> | null = null;
  const api = await mockProductApi(page, [
    {
      method: 'POST',
      path: '/api/workspace-apps/',
      response: async (request) => {
        createPayload = await request.postDataJSON();
        return {
          id: 'app-preview-metrics',
          org_id: String(e2eUser.org_id),
          key: createPayload?.key ?? 'local-preview-orbit',
          name: createPayload?.name ?? 'Preview app',
          description: createPayload?.description ?? null,
          renderer_key: createPayload?.renderer_key ?? 'generated-ui-app',
          visual_spec: createPayload?.visual_spec ?? {},
          metadata: createPayload?.metadata ?? {},
          created_by_user_id: e2eUser.id,
          anchor_user_id: e2eUser.id,
          archived_at: null,
          created_at: '2026-05-01T10:00:00Z',
          updated_at: '2026-05-01T10:00:00Z',
          active_version: {
            id: 1,
            app_id: 'app-preview-metrics',
            version: 1,
            renderer_key: createPayload?.renderer_key ?? 'generated-ui-app',
            source_kind: createPayload?.source_kind ?? 'json',
            source_code: createPayload?.source_code ?? '{}',
            manifest: createPayload?.manifest ?? {},
            created_by_user_id: e2eUser.id,
            created_at: '2026-05-01T10:00:00Z',
          },
          contract_validation: { ok: true },
        };
      },
    },
  ]);

  await page.goto('/cortex');
  await page.getByRole('button', { name: /Preview/ }).click();
  await page.getByRole('button', { name: 'Add app' }).click();

  await expect(page.getByText('Dummy apps')).toBeVisible();
  await expect.poll(() => createPayload).not.toBeNull();
  expect(createPayload).toMatchObject({
    renderer_key: 'generated-ui-app',
    source_kind: 'json',
    anchor_user_id: e2eUser.id,
    metadata: { local_preview_kind: 'local-preview-orbit-app' },
  });
  expect(createPayload?.manifest).toMatchObject({
    contract_version: 1,
    design_contract: { kit: 'constellation-app-kit' },
  });
  await expectNoUnhandledApiRequests(api);
});
