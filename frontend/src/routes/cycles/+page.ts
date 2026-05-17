import { dev } from '$app/environment';
import { redirect } from '@sveltejs/kit';
import { buildCortexWorkspacePageHref } from '$lib/features/cortex/domain/workspacePageModal';

export const load = ({ url }) => {
  if (dev && url.searchParams.get('preview') === '1') return {};
  throw redirect(307, buildCortexWorkspacePageHref('cycles', url.searchParams));
};
