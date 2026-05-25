import { resizeComposerTextareaToContent } from './composerTextareaSizing';

export const WORKSPACE_COMPOSER_MIN_HEIGHT = 40;
export const WORKSPACE_COMPOSER_BOTTOM_MARGIN_MIN = 14;
export const WORKSPACE_COMPOSER_BOTTOM_MARGIN_MAX = 26;
export const WORKSPACE_COMPOSER_BOTTOM_MARGIN_RATIO = 0.024;
export const WORKSPACE_COMPOSER_CHROME_HEIGHT = 96;

export function clampWorkspaceComposerValue(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function getWorkspaceComposerViewportHeight(win: Pick<Window, 'innerHeight' | 'visualViewport'>): number {
  return win.visualViewport?.height ?? win.innerHeight;
}

export function getWorkspaceComposerBottomMargin(viewportHeight: number): number {
  return clampWorkspaceComposerValue(
    viewportHeight * WORKSPACE_COMPOSER_BOTTOM_MARGIN_RATIO,
    WORKSPACE_COMPOSER_BOTTOM_MARGIN_MIN,
    WORKSPACE_COMPOSER_BOTTOM_MARGIN_MAX,
  );
}

export function getWorkspaceComposerMaxHeight(viewportHeight: number): number {
  return Math.max(
    WORKSPACE_COMPOSER_MIN_HEIGHT,
    Math.floor(viewportHeight - getWorkspaceComposerBottomMargin(viewportHeight) - WORKSPACE_COMPOSER_CHROME_HEIGHT),
  );
}

export function getWorkspaceComposerTextareaHeight(scrollHeight: number, maxHeight: number): number {
  return Math.max(WORKSPACE_COMPOSER_MIN_HEIGHT, Math.min(scrollHeight, maxHeight));
}

export function applyWorkspaceComposerTextareaHeight(
  textareaEl: HTMLTextAreaElement | undefined,
  maxHeight: number,
): void {
  resizeComposerTextareaToContent(textareaEl, {
    maxHeight,
    minHeight: WORKSPACE_COMPOSER_MIN_HEIGHT,
  });
}
