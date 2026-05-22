import type {
  ConstellationActivity,
  ConstellationAstrePresence,
  ConstellationScale,
  ConstellationShape,
  ConstellationSignalCue,
  ConstellationSignalIcon,
  ConstellationSignalPresence,
  ConstellationSignalState,
  ConstellationSignalTreatment,
  ConstellationTone,
} from '$lib/components/constellation/constellationTypes';

const SEMANTIC_ZOOM_DETAIL_MIN = 0.74;
const SEMANTIC_ZOOM_SUMMARY_MIN = 0.52;
const SEMANTIC_ZOOM_SYMBOL_MIN = 0.34;

type PrimitiveStyleValue = string | number | null | undefined;
export type SemanticZoomLevel = 'detail' | 'summary' | 'symbol' | 'glyph';
export type CortexThemeMode = 'light' | 'dark' | string;

export type PrimitiveBlobVisual = {
  id: string;
  text: string;
  accent: string;
  x: number;
  y: number;
  width: number;
  height: number;
  tone: ConstellationTone;
  shape: ConstellationShape;
  scale: ConstellationScale;
  state: ConstellationSignalState;
  cue: ConstellationSignalCue;
  presence: ConstellationSignalPresence;
  treatment: ConstellationSignalTreatment;
  icon: ConstellationSignalIcon;
  attachmentCount: number;
};

export type PrimitiveAstreVisual = {
  id: string;
  letter: string;
  owner: string;
  accent: string;
  x: number;
  y: number;
  size: number;
  tone: ConstellationTone;
  scale: ConstellationScale;
  activity: ConstellationActivity;
  presence: ConstellationAstrePresence;
  archivedCount: number;
};

export type PrimitivePinVisual = {
  id: string;
  pinId: string;
  label: string;
  accent: string;
  createdByUserId: string | null;
  canEdit: boolean;
  canMove: boolean;
  x: number;
  y: number;
};

export type PrimitiveAppVisual = {
  id: string;
  name: string;
  description: string | null;
  rendererKey: string;
  visualSpec: Record<string, any>;
  stateKey: string;
  accent: string;
  anchorKey: string;
  anchorType: 'user' | 'pin';
  anchorId: string;
  x: number;
  y: number;
  opacity: number;
  active: boolean;
  floatX: number;
  floatY: number;
  floatDuration: number;
  floatDelay: number;
};

export type WorkspaceAppCollisionObstacle = {
  id: string;
  x: number;
  y: number;
};

export type PrimitiveOrbitLaneRing = {
  id: string;
  rx: number;
  ry: number;
  opacity: number;
  role?: 'app' | 'thread';
};

export type PrimitiveOrbitLaneSpoke = {
  id: string;
  angle: number;
  fromRadius: number;
  toRadius: number;
  opacity: number;
};

export type PrimitiveOrbitLaneDot = {
  id: string;
  x: number;
  y: number;
  size: number;
  opacity: number;
  state: ConstellationSignalState;
  cue: ConstellationSignalCue;
};

export type PrimitiveOrbitLaneVisual = {
  id: string;
  kind: 'user' | 'pin';
  accent: string;
  x: number;
  y: number;
  outerRx: number;
  outerRy: number;
  rings: PrimitiveOrbitLaneRing[];
  spokes: PrimitiveOrbitLaneSpoke[];
  dots: PrimitiveOrbitLaneDot[];
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function primitiveStyle(declarations: Record<string, PrimitiveStyleValue>) {
  return Object.entries(declarations)
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(([key, value]) => `${key}: ${value}`)
    .join('; ');
}

export function orbitLaneFade(duration: number) {
  const reduced = typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  return { duration: reduced ? 0 : duration };
}

export function semanticZoomLevelForScale(scale: number): SemanticZoomLevel {
  if (scale >= SEMANTIC_ZOOM_DETAIL_MIN) return 'detail';
  if (scale >= SEMANTIC_ZOOM_SUMMARY_MIN) return 'summary';
  if (scale >= SEMANTIC_ZOOM_SYMBOL_MIN) return 'symbol';
  return 'glyph';
}

export function primitiveOrbitLaneStyle(lane: PrimitiveOrbitLaneVisual) {
  return primitiveStyle({
    left: `${lane.x}px`,
    top: `${lane.y}px`,
    width: '0px',
    height: '0px',
    '--orbit-lane-accent': lane.accent,
  });
}

export function primitiveOrbitLaneRingStyle(
  lane: PrimitiveOrbitLaneVisual,
  ring: PrimitiveOrbitLaneRing,
  themeMode: CortexThemeMode,
) {
  const opacity = themeMode === 'light'
    ? lane.kind === 'pin'
      ? clamp(ring.opacity * 1.05 + 0.035, 0.12, 0.28)
      : clamp(ring.opacity * 1.45 + 0.08, 0.24, 0.62)
    : lane.kind === 'pin'
      ? ring.opacity
      : clamp(ring.opacity * 1.22 + 0.035, 0.18, 0.66);

  return primitiveStyle({
    left: `${-ring.rx}px`,
    top: `${-ring.ry}px`,
    width: `${ring.rx * 2}px`,
    height: `${ring.ry * 2}px`,
    opacity,
    '--orbit-ring-weight': ring.role === 'app' ? '1.25px' : '1px',
    '--orbit-ring-alpha': ring.role === 'app' ? '62%' : '54%',
  });
}

export function primitiveOrbitLaneSpokeStyle(
  lane: PrimitiveOrbitLaneVisual,
  spoke: PrimitiveOrbitLaneSpoke,
  themeMode: CortexThemeMode,
) {
  const angle = (spoke.angle * Math.PI) / 180;
  const midRadius = (spoke.fromRadius + spoke.toRadius) / 2;
  const opacity = themeMode === 'light'
    ? lane.kind === 'pin'
      ? clamp(spoke.opacity * 1.02 + 0.03, 0.1, 0.24)
      : clamp(spoke.opacity * 1.5 + 0.08, 0.24, 0.62)
    : lane.kind === 'pin'
      ? spoke.opacity
      : clamp(spoke.opacity * 1.28 + 0.04, 0.18, 0.68);

  return primitiveStyle({
    left: `${Math.cos(angle) * midRadius}px`,
    top: `${Math.sin(angle) * midRadius}px`,
    width: `${Math.max(0, spoke.toRadius - spoke.fromRadius)}px`,
    opacity,
    '--orbit-spoke-rotation': `${spoke.angle}deg`,
  });
}

export function primitiveOrbitLaneDotStyle(
  lane: PrimitiveOrbitLaneVisual,
  dot: PrimitiveOrbitLaneDot,
  themeMode: CortexThemeMode,
) {
  const opacity = themeMode === 'light'
    ? lane.kind === 'pin'
      ? clamp(dot.opacity * 0.86, 0.32, 0.58)
      : clamp(dot.opacity * 1.12, 0.78, 1)
    : dot.opacity;

  return primitiveStyle({
    left: `${dot.x}px`,
    top: `${dot.y}px`,
    width: `${dot.size}px`,
    height: `${dot.size}px`,
    opacity,
    '--orbit-dot-flare': `${lane.kind === 'pin' ? Math.max(4, dot.size * 2.4) : Math.max(8, dot.size * 4.4)}px`,
  });
}
