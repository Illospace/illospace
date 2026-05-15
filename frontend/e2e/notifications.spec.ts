import { expect, test } from '@playwright/test';
import { expectNoUnhandledApiRequests, mockProductApi } from './fixtures';

test('workspace notifications show unread chat and workspace attention from the real shell', async ({ page }) => {
  const api = await mockProductApi(page);

  await page.goto('/cortex');
  await page.getByRole('button', { name: 'Notifications' }).click();

  await expect(page.getByRole('menu', { name: 'Unread notifications' })).toBeVisible();
  await expect(page.getByText('Run cancellation needs review')).toBeVisible();
  await expect(page.getByText('Grace mentioned you in the team room.')).toBeVisible();
  await expectNoUnhandledApiRequests(api);
});
