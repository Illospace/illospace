export type ShortcutMenuPlacement = 'above' | 'below';

export type ShortcutMenuGeometry = {
  placement: ShortcutMenuPlacement;
  left: number;
  width: number;
  maxHeight: number;
  top: number | null;
  bottom: number | null;
};

export type ShortcutMenuGeometryOptions = {
  placement?: ShortcutMenuPlacement;
  preferredHeight?: number;
  minHeight?: number;
  maxHeight?: number;
  viewportGap?: number;
  anchorGap?: number;
  minWidth?: number;
  maxWidth?: number;
};

type AnchorRect = Pick<DOMRect, 'top' | 'bottom' | 'left' | 'width'>;

const DEFAULT_PREFERRED_HEIGHT = 180;
const DEFAULT_MIN_HEIGHT = 96;
const DEFAULT_MAX_HEIGHT = 260;
const DEFAULT_VIEWPORT_GAP = 12;
const DEFAULT_ANCHOR_GAP = 8;

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function cssLength(value: number | null) {
  return value === null ? 'auto' : `${value}px`;
}

export function shortcutMenuPortal(node: HTMLElement) {
  if (typeof document === 'undefined') return {};
  document.body.appendChild(node);

  return {
    destroy() {
      node.remove();
    },
  };
}

export function anchoredShortcutMenuGeometry(
  rect: AnchorRect,
  viewportWidth: number,
  viewportHeight: number,
  options: ShortcutMenuGeometryOptions = {},
): ShortcutMenuGeometry {
  const viewportGap = options.viewportGap ?? DEFAULT_VIEWPORT_GAP;
  const anchorGap = options.anchorGap ?? DEFAULT_ANCHOR_GAP;
  const preferredHeight = options.preferredHeight ?? DEFAULT_PREFERRED_HEIGHT;
  const minHeight = options.minHeight ?? DEFAULT_MIN_HEIGHT;
  const maxHeight = options.maxHeight ?? DEFAULT_MAX_HEIGHT;
  const availableWidth = Math.max(0, viewportWidth - viewportGap * 2);
  const maxWidth = Math.min(options.maxWidth ?? availableWidth, availableWidth);
  const minWidth = Math.min(options.minWidth ?? 0, maxWidth);
  const width = clamp(rect.width, minWidth, maxWidth);
  const maxLeft = Math.max(viewportGap, viewportWidth - viewportGap - width);
  const left = clamp(rect.left, viewportGap, maxLeft);
  const spaceAbove = Math.max(0, rect.top - viewportGap);
  const spaceBelow = Math.max(0, viewportHeight - rect.bottom - viewportGap);

  let placement = options.placement ?? 'above';
  if (placement === 'above' && spaceAbove < preferredHeight && spaceBelow > spaceAbove) {
    placement = 'below';
  } else if (placement === 'below' && spaceBelow < preferredHeight && spaceAbove > spaceBelow) {
    placement = 'above';
  }

  const availableSpace = placement === 'above' ? spaceAbove : spaceBelow;

  return {
    placement,
    left,
    width,
    maxHeight: clamp(availableSpace - anchorGap, minHeight, maxHeight),
    top: placement === 'below' ? rect.bottom + anchorGap : null,
    bottom: placement === 'above' ? viewportHeight - rect.top + anchorGap : null,
  };
}

export function shortcutMenuCssVariables(geometry: ShortcutMenuGeometry, prefix: string) {
  return [
    `--${prefix}-dropdown-left:${geometry.left}px`,
    `--${prefix}-dropdown-width:${geometry.width}px`,
    `--${prefix}-dropdown-max-height:${geometry.maxHeight}px`,
    `--${prefix}-dropdown-top:${cssLength(geometry.top)}`,
    `--${prefix}-dropdown-bottom:${cssLength(geometry.bottom)}`,
  ].join(';');
}
