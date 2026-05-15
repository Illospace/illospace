import { expect, test } from '@playwright/test';
import { expectNoUnhandledApiRequests, mockProductApi } from './fixtures';

test('system runtime page shows access, model, and memory readiness from runtime settings', async ({ page }) => {
  const api = await mockProductApi(page);

  await page.goto('/system');

  await expect(page.getByText('System').first()).toBeVisible();
  await expect(page.getByText('OpenAI API key').first()).toBeVisible();
  await expect(page.getByText('Choose Models')).toBeVisible();
  await expect(page.getByText('text-embedding-3-small')).toBeVisible();
  await expectNoUnhandledApiRequests(api);
});
