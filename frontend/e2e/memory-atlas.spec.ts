import { expect, test } from '@playwright/test';
import { expectNoUnhandledApiRequests, mockProductApi } from './fixtures';

const memories = [
  {
    id: 101,
    content: 'Vault rotation policy is required before release.',
    memory_type: 'decision',
    salience: 9,
    tags: ['vault', 'release'],
    visibility: 'org',
    created_at: '2026-05-01T10:00:00Z',
    updated_at: '2026-05-01T10:00:00Z',
  },
  {
    id: 102,
    content: 'Cortex run cancellation must be visible from the active thread.',
    memory_type: 'lesson',
    salience: 6,
    tags: ['runs'],
    visibility: 'private',
    created_at: '2026-05-01T10:01:00Z',
    updated_at: '2026-05-01T10:01:00Z',
  },
];

test('memory atlas renders recalled knowledge and filters locally', async ({ page }) => {
  const api = await mockProductApi(page, [
    {
      path: '/api/memory/graph-similarity',
      response: {
        nodes: memories,
        edges: [{ source_id: 101, target_id: 102, relation: 'supports', weight: 0.7 }],
        similarity_edges: [{ source_id: 101, target_id: 102, similarity: 0.72 }],
      },
    },
    { path: /^\/api\/memory\/\d+$/, response: memories[0] },
    { path: /^\/api\/memory\/\d+\/neighborhood$/, response: [] },
  ]);

  await page.goto('/memory');

  await expect(page.getByText('Memory atlas').first()).toBeVisible();
  await expect(page.getByText('Vault rotation policy is required before release.')).toBeVisible();
  await expect(page.getByText('Cortex run cancellation must be visible from the active thread.')).toBeVisible();

  await page.getByPlaceholder('Search memories by content or tags...').fill('vault');
  await expect(page.getByText('Vault rotation policy is required before release.')).toBeVisible();
  await expect(page.getByText('Cortex run cancellation must be visible from the active thread.')).toBeHidden();
  await expectNoUnhandledApiRequests(api);
});
