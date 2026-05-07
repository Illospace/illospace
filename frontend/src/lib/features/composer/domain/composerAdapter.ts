import type { Snippet } from 'svelte';

import type { ConstellationIconName } from '$lib/components/constellation';
import { getRunHint } from '$lib/utils/backgroundRun';

export const CORTEX_WORKSPACE_COMPOSER_MODES = ['workspace', 'thread'] as const;
export type CortexWorkspaceComposerMode = (typeof CORTEX_WORKSPACE_COMPOSER_MODES)[number];

export const CORTEX_WORKSPACE_COMPOSER_TONES = ['amber', 'spectral'] as const;
export type CortexWorkspaceComposerTone = (typeof CORTEX_WORKSPACE_COMPOSER_TONES)[number];

export const CORTEX_WORKSPACE_COMPOSER_ACTION_STATES = ['idle', 'working'] as const;
export type CortexWorkspaceComposerActionState =
  (typeof CORTEX_WORKSPACE_COMPOSER_ACTION_STATES)[number];

export interface CortexWorkspaceComposerOrigin {
  x: number;
  y: number;
}

export interface CortexWorkspaceComposerContextLike {
  screenX: number;
  screenY: number;
  worldX?: number;
  worldY?: number;
}

export interface CortexWorkspaceComposerAttachment {
  id?: string | number;
  filename?: string;
  label?: string;
  url?: string;
  alt?: string;
  type?: string;
  content_type?: string;
}

export interface CortexWorkspaceComposerIntentOption {
  value: string;
  label: string;
  description?: string;
  icon?: ConstellationIconName;
}

export interface CortexWorkspaceComposerSettingsGroup {
  key: string;
  label: string;
  options: readonly CortexWorkspaceComposerIntentOption[];
  value?: string;
  ariaLabel?: string;
}

export interface WorkspaceComposerAdapterProps {
  mode?: CortexWorkspaceComposerMode;
  tone?: CortexWorkspaceComposerTone;
  actionStyle?: string;
  kicker?: string;
  placeholder: string;
  hint?: string;
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  actionState?: CortexWorkspaceComposerActionState;
  canSubmit?: boolean;
  allowSubmitWhileWorking?: boolean;
  onSubmit?: (value: string) => void;
  onStop?: () => void;
  attachLabel?: string;
  sendLabel?: string;
  stopLabel?: string;
  intentOptions?: readonly CortexWorkspaceComposerIntentOption[];
  intentValue?: string;
  onIntentChange?: (value: string) => void;
  intentAriaLabel?: string;
  secondaryIntentOptions?: readonly CortexWorkspaceComposerIntentOption[];
  secondaryIntentValue?: string;
  onSecondaryIntentChange?: (value: string) => void;
  secondaryIntentAriaLabel?: string;
  settingsGroups?: readonly CortexWorkspaceComposerSettingsGroup[];
  onSettingsChange?: (key: string, value: string) => void;
  settingsAriaLabel?: string;
  attachments?: readonly CortexWorkspaceComposerAttachment[];
  onAttach?: () => void;
  onRemoveAttachment?: (index: number) => void;
  onPaste?: (event: ClipboardEvent) => void;
  onDrop?: (event: DragEvent) => void;
  onDragOver?: (event: DragEvent) => void;
  onDragLeave?: (event: DragEvent) => void;
  onKeydown?: (event: KeyboardEvent) => void;
  onthreadintent?: (origin: CortexWorkspaceComposerOrigin) => void;
  context?: CortexWorkspaceComposerContextLike | null;
  disabled?: boolean;
  isDragOver?: boolean;
  className?: string;
  editor?: Snippet;
  attachmentsSlot?: Snippet;
  leadingControls?: Snippet;
  extraLeadingControls?: Snippet;
  trailingControls?: Snippet;
  supporting?: Snippet;
}

export function getWorkspaceComposerActionLabel(
  actionState: CortexWorkspaceComposerActionState,
  sendLabel = 'Send',
  stopLabel = 'Stop generation',
): string {
  return actionState === 'working' ? stopLabel : sendLabel;
}

export function getWorkspaceComposerOrigin(
  context?: CortexWorkspaceComposerContextLike | null,
): CortexWorkspaceComposerOrigin | null {
  if (!context) return null;
  return {
    x: context.screenX,
    y: context.screenY,
  };
}

export function getWorkspaceComposerRunHint(content: string, attachmentCount = 0): string {
  return getRunHint(content, attachmentCount);
}
