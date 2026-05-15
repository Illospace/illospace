import { expect, test } from '@playwright/test';
import { e2eUser, expectNoUnhandledApiRequests, mockProductApi } from './fixtures';

test('first workspace setup creates an owner and sends them into onboarding', async ({ page }) => {
  let registeredPayload: Record<string, unknown> | null = null;
  let authenticated = false;

  const api = await mockProductApi(
    page,
    [
      { path: '/api/me', response: () => (authenticated ? e2eUser : null) },
      {
        path: '/api/auth/setup-check',
        response: {
          setup_required: true,
          default_org: null,
          requested_org: null,
        },
      },
      {
        method: 'POST',
        path: '/api/register',
        response: async (request) => {
          registeredPayload = await request.postDataJSON();
          authenticated = true;
          return e2eUser;
        },
      },
    ],
    { user: null },
  );

  await page.goto('/login?view=register&mode=create');
  await page.getByLabel('Workspace name').fill('Red Team Lab');
  await page.getByLabel('Your name').fill('Ada Lovelace');
  await page.getByLabel('Email').fill('ada@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Create workspace' }).click();

  await expect(page).toHaveURL(/\/onboarding/);
  expect(registeredPayload).toMatchObject({
    name: 'Ada Lovelace',
    email: 'ada@example.test',
    workspace_mode: 'create',
    org_name: 'Red Team Lab',
  });
  await expectNoUnhandledApiRequests(api);
});
