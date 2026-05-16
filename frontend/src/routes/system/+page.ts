import { redirect } from '@sveltejs/kit';
import { buildCortexWorkspacePageHref } from '$lib/features/cortex/domain/workspacePageModal';

export const load = ({ url }) => {
  throw redirect(307, buildCortexWorkspacePageHref('system', url.searchParams));
};
