import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';
import type {
  ProjectFileKind,
  ProjectPreviewLayerKey,
} from '$lib/features/threads/domain/projectDraftStatePresenter';

export function projectFileIconName(kind: ProjectFileKind): ConstellationIconName {
  if (kind === 'image') return 'image';
  if (kind === 'pdf') return 'pdf';
  if (kind === 'spreadsheet' || kind === 'data') return 'database';
  if (kind === 'code' || kind === 'graph') return 'code';
  if (kind === 'video') return 'video';
  if (kind === 'archive') return 'archive';
  if (kind === 'markdown' || kind === 'document') return 'document';
  return 'file';
}

export function finalLayerSourceLabel(key: ProjectPreviewLayerKey): string {
  if (key === 'root') return 'project root';
  if (key === 'base') return 'thread base';
  return 'thread draft';
}

export function canEmbedProjectFileKind(kind: ProjectFileKind): boolean {
  return kind === 'image' || kind === 'pdf' || kind === 'video';
}

export function projectPreviewPrimaryTitle(
  showPreviewModeTabs: boolean,
  layer: ProjectPreviewLayerKey,
): string {
  if (showPreviewModeTabs) return 'Final';
  if (layer === 'root') return 'Project root';
  if (layer === 'base') return 'Thread base';
  return 'Thread draft';
}

export function diffMarker(kind: 'context' | 'removed' | 'added'): string {
  if (kind === 'added') return '+';
  if (kind === 'removed') return '-';
  return ' ';
}
