import { expect, test } from '@playwright/test';
import { expectNoUnhandledApiRequests, mockProductApi } from './fixtures';

test('skills library renders bundle-backed skills and attention filtering', async ({ page }) => {
  const api = await mockProductApi(page, [
    {
      path: '/api/skills/enhanced',
      response: [
        {
          skill: {
            id: 1,
            name: 'test-suite-audit',
            description: 'Audit tests for product survivability.',
            procedure: 'Inspect journeys, contracts, and adversarial coverage before pruning duplicate checks.',
            version: 3,
            maturity: 'trusted',
            use_count: 12,
            partial_count: 1,
            avg_duration_sec: 42,
            last_used: '2026-05-01T10:00:00Z',
            pitfalls: ['Do not replace product journeys with route smoke tests.'],
            refinements: [],
            triggers: [{ direction: 'for', pattern: 'test suite audit' }],
            guardrails: [{ text: 'Prefer behavior-level evidence.', severity: 'warning' }],
            model_tier: 'high',
            thinking_tier: 'high',
          },
          package: {
            package_kind: 'bundle',
            is_bundle_backed: true,
            display_name: 'Test Suite Audit',
            source_kind: 'repo',
            trust_level: 'trusted',
            visibility: 'org',
            semver: '1.0.0',
            enabled: true,
            asset_count: 2,
            assets: [
              { path: 'SKILL.md', asset_kind: 'procedure', mime_type: 'text/markdown', size_bytes: 1200 },
              { path: 'evals/routing.jsonl', asset_kind: 'eval', mime_type: 'application/jsonl', size_bytes: 240 },
            ],
          },
          needs_attention: false,
          convert_to_bundle_available: false,
        },
      ],
    },
  ]);

  await page.goto('/skills');

  await expect(page.getByLabel('Skills')).toBeVisible();
  await expect(page.getByText('test-suite-audit')).toBeVisible();
  await expect(page.getByText('Audit tests for product survivability.')).toBeVisible();
  await page.getByLabel('Search skills').fill('survivability');
  await expect(page.getByText('test-suite-audit')).toBeVisible();
  await expectNoUnhandledApiRequests(api);
});
