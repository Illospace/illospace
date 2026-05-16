import type { Snippet } from 'svelte';

export const CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT = 'constellation:workspace-page-modal';

export type ConstellationPageFrameModalRefreshAction = {
  label: string;
  disabled?: boolean;
  onclick: () => void | Promise<void>;
};

export type ConstellationPageFrameModalContext = {
  embedded: true;
  registerActions: (actions: Snippet | undefined) => () => void;
  registerRefreshAction: (action: ConstellationPageFrameModalRefreshAction | undefined) => () => void;
};
