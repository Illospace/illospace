import { expect, test } from '@playwright/test';
import { expectNoUnhandledApiRequests, mockProductApi } from './fixtures';

test('workspace chat dock hydrates the team room from chat bootstrap', async ({ page }) => {
  const api = await mockProductApi(page);

  await page.goto('/cortex');
  await page.getByLabel('Workspace chat').hover();

  await expect(page.getByLabel('Chat')).toBeVisible({ timeout: 6_000 });
  await expect(page.getByRole('button', { name: 'Team' })).toBeVisible();
  await expect(page.getByText('Can someone check the run cancellation path?')).toBeVisible();
  await expectNoUnhandledApiRequests(api);
});
