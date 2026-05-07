export type ZoomTransformLike = {
  x: number;
  y: number;
  k: number;
  invert(point: [number, number]): [number, number];
};

export type WorkspaceScenePoint = {
  worldX: number;
  worldY: number;
  screenX: number;
  screenY: number;
};

export function workspacePointFromClientRect(
  rect: DOMRect,
  transform: ZoomTransformLike,
  clientX: number,
  clientY: number,
): WorkspaceScenePoint {
  const localX = clientX - rect.left;
  const localY = clientY - rect.top;
  const [worldX, worldY] = transform.invert([localX, localY]);
  return {
    worldX,
    worldY,
    screenX: clientX,
    screenY: clientY,
  };
}

export function primitiveOverlayTransformStyle(transform: ZoomTransformLike) {
  return `transform: translate(${transform.x}px, ${transform.y}px) scale(${transform.k});`;
}

export function applyPrimitiveOverlayTransform(element: HTMLElement | undefined, transform: ZoomTransformLike) {
  if (!element) return;
  element.style.transform = `translate3d(${transform.x}px, ${transform.y}px, 0) scale(${transform.k})`;
}
