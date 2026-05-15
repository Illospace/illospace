import { expect, test } from '@playwright/test';

test('vault preview exercises the inventory, missing-key, and agent-token surfaces without real secrets', async ({ page }) => {
  await page.goto('/vault?preview=1');

  await expect(page.getByLabel('Vault entries')).toBeVisible();
  await expect(page.getByText('OPENAI_API_KEY').first()).toBeVisible();
  await expect(page.getByText('BRAVE_SEARCH_API_KEY')).toBeVisible();
  await expect(page.getByText('Hermes')).toBeVisible();

  await page.getByLabel('Search vault').fill('stripe');
  await expect(page.getByText('STRIPE_WEBHOOK_SECRET')).toBeVisible();
  await expect(page.getByText('OPENAI_API_KEY').first()).toBeHidden();
});
