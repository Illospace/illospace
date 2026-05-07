import type { OrbitNode, ScenePoint } from '../domain/workspaceSceneState';

export const BIRTH_DURATION_MS = 720;

function clamp01(value: number) {
  return Math.min(Math.max(value, 0), 1);
}

function easeCubicOut(progress: number) {
  return 1 - (1 - progress) ** 3;
}

function interpolateNumber(from: number, to: number, t: number) {
  return from + (to - from) * t;
}

export function clearBirthSceneState(node: OrbitNode) {
  node._sceneState = 'free';
  delete node._birthFromX;
  delete node._birthFromY;
  delete node._birthStartedAt;
  delete node._birthDurationMs;
}

export function collapseBirthAnimation(node: OrbitNode | null | undefined) {
  if (!node || node._sceneState !== 'birth') return;
  clearBirthSceneState(node);
}

export function birthRenderPosition(node: OrbitNode, now = performance.now()): ScenePoint | null {
  if (node._sceneState !== 'birth') return null;
  if (
    typeof node._birthFromX !== 'number'
    || typeof node._birthFromY !== 'number'
    || typeof node._birthStartedAt !== 'number'
  ) {
    clearBirthSceneState(node);
    return null;
  }

  const duration = node._birthDurationMs ?? BIRTH_DURATION_MS;
  const progress = clamp01((now - node._birthStartedAt) / duration);
  const eased = easeCubicOut(progress);
  const x = interpolateNumber(node._birthFromX, node.x, eased);
  const y = interpolateNumber(node._birthFromY, node.y, eased);

  if (progress >= 1) {
    clearBirthSceneState(node);
    return { x: node.x, y: node.y };
  }

  return { x, y };
}

export function startBirthLifecycle(
  node: OrbitNode,
  birthFrom: ScenePoint,
  orbitPoint: ScenePoint,
  now = performance.now(),
) {
  node.x = orbitPoint.x;
  node.y = orbitPoint.y;
  node.vx = (orbitPoint.x - birthFrom.x) * 0.015;
  node.vy = (orbitPoint.y - birthFrom.y) * 0.015;
  node._sceneState = 'birth';
  node._birthFromX = birthFrom.x;
  node._birthFromY = birthFrom.y;
  node._birthStartedAt = now;
  node._birthDurationMs = BIRTH_DURATION_MS;
}
