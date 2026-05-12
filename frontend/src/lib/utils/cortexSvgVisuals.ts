import type {
  ConstellationActivity,
  ConstellationScale,
  ConstellationShape,
  ConstellationSignalIcon,
  ConstellationSignalTreatment,
  ConstellationTone,
} from '$lib/components/constellation/constellationTypes';

const COLORS: Record<string, string> = {
  idle: '#f0f0fa',
  working: '#f0f0fa',
  done: '#f0f0fa',
};
export function visualStatus(status: string | undefined) {
  switch (status) {
    case 'queued':
    case 'running':
    case 'active':
    case 'working':
      return 'working';
    case 'completed':
    case 'done':
      return 'done';
    default:
      return 'idle';
  }
}
export function statusColor(status: string | undefined) {
  return COLORS[visualStatus(status)] || COLORS.idle;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function normalizeHexColor(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(trimmed)) return null;
  if (trimmed.length === 4) {
    return `#${trimmed[1]}${trimmed[1]}${trimmed[2]}${trimmed[2]}${trimmed[3]}${trimmed[3]}`;
  }
  return trimmed;
}


export const ASTRE_ARCHIVE_DOT_PRESETS = [
  {
    layer: 'outer',
    waypoints: [
      { top: 12, left: 86 },
      { top: 30, left: 60 },
      { top: 76, left: 78 },
      { top: 88, left: 28 },
    ],
    size: 4,
    variant: 'a',
    delay: -4.2,
  },
  {
    layer: 'inner',
    waypoints: [
      { top: 76, left: 74 },
      { top: 36, left: 66 },
      { top: 24, left: 30 },
      { top: 64, left: 22 },
    ],
    size: 3,
    variant: 'b',
    delay: -11.6,
  },
  {
    layer: 'outer',
    waypoints: [
      { top: 102, left: 72 },
      { top: 70, left: 94 },
      { top: 34, left: 56 },
      { top: 18, left: 18 },
    ],
    size: 3,
    variant: 'c',
    delay: -17.8,
  },
  {
    layer: 'inner',
    waypoints: [
      { top: 24, left: 22 },
      { top: 20, left: 64 },
      { top: 58, left: 76 },
      { top: 74, left: 38 },
    ],
    size: 3,
    variant: 'b',
    delay: -8.4,
  },
  {
    layer: 'outer',
    waypoints: [
      { top: 92, left: 10 },
      { top: 56, left: 18 },
      { top: 24, left: 44 },
      { top: 68, left: 94 },
    ],
    size: 3,
    variant: 'a',
    delay: -14.1,
  },
] as const;


export const BUBBLE_SHELL_COLOR = '#050910';
export const CONSTELLATION_TEXT_COLOR = '#f0f0fa';
const LIGHT_BLOB_CORE_COLOR = '#f8f4f1';
const LIGHT_CONSTELLATION_TEXT_COLOR = '#312c36';

export function wrapTextForBubble(title: string, radius: number): string[] {
  const text = radius >= 55 ? title : (title.length <= 25 ? title : title.slice(0, 22) + '…');
  const charWidth = 6.5;
  const fs = fontSize(radius);
  const scale = fs / 13;
  const maxLineWidth = radius * 1.6;
  const maxCharsPerLine = Math.max(4, Math.floor(maxLineWidth / (charWidth * scale)));
  const maxLines = radius < 35 ? 1 : radius < 50 ? 2 : radius < 70 ? 3 : radius < 90 ? 4 : 5;
  const words = text.split(/\s+/);
  const lines: string[] = [];
  let currentLine = '';
  for (const word of words) {
    const test = currentLine ? currentLine + ' ' + word : word;
    if (test.length > maxCharsPerLine && currentLine) {
      lines.push(currentLine);
      currentLine = word;
      if (lines.length >= maxLines) break;
    } else {
      currentLine = test;
    }
  }
  if (currentLine && lines.length < maxLines) lines.push(currentLine);
  if (lines.length > 0) {
    const last = lines[lines.length - 1];
    if (last.length > maxCharsPerLine) lines[lines.length - 1] = last.slice(0, maxCharsPerLine - 1) + '…';
  }
  if (lines.length >= maxLines && words.length > lines.join(' ').split(/\s+/).length) {
    const last = lines[lines.length - 1];
    if (!last.endsWith('…')) lines[lines.length - 1] = last.slice(0, maxCharsPerLine - 1) + '…';
  }
  return lines.length ? lines : [title.slice(0, maxCharsPerLine - 1) + '…'];
}

const BLOB_SHAPES: ConstellationShape[] = ['alpha', 'beta', 'gamma', 'delta'];
const fontSize = (r: number) => Math.max(10, Math.min(15, r * 0.26));

// ── PRNG + Blob Path (from cortex-physics.js) ──────────────
export function seedHash(str: string): number {
  let h = 0;
  for (let i = 0; i < str.length; i++) { h = ((h << 5) - h + str.charCodeAt(i)) | 0; }
  return h;
}
export function seededRandom(seed: number) {
  let s = seed;
  return () => { s = (s * 16807 + 0) % 2147483647; return (s - 1) / 2147483646; };
}
export function generateBlobPath(radius: number, seed: string, numPoints = 7): string {
  const rng = seededRandom(Math.abs(seedHash(String(seed))) + 1);
  const points: { x: number; y: number }[] = [];
  for (let i = 0; i < numPoints; i++) {
    const angle = (i / numPoints) * Math.PI * 2;
    const r = radius * (1 + (rng() - 0.5) * 0.36);
    points.push({ x: Math.cos(angle) * r, y: Math.sin(angle) * r });
  }
  let d = '';
  for (let i = 0; i < numPoints; i++) {
    const p0 = points[(i - 1 + numPoints) % numPoints];
    const p1 = points[i];
    const p2 = points[(i + 1) % numPoints];
    const p3 = points[(i + 2) % numPoints];
    if (i === 0) d += `M${p1.x.toFixed(2)},${p1.y.toFixed(2)}`;
    const cp1x = p1.x + (p2.x - p0.x) / 6;
    const cp1y = p1.y + (p2.y - p0.y) / 6;
    const cp2x = p2.x - (p3.x - p1.x) / 6;
    const cp2y = p2.y - (p3.y - p1.y) / 6;
    d += ` C${cp1x.toFixed(2)},${cp1y.toFixed(2)} ${cp2x.toFixed(2)},${cp2y.toFixed(2)} ${p2.x.toFixed(2)},${p2.y.toFixed(2)}`;
  }
  return d + 'Z';
}
export function lightenColor(hex: string, amount = 0.3): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgb(${Math.round(r + (255 - r) * amount)},${Math.round(g + (255 - g) * amount)},${Math.round(b + (255 - b) * amount)})`;
}

function hexToRgb(hex: string) {
  return {
    r: parseInt(hex.slice(1, 3), 16),
    g: parseInt(hex.slice(3, 5), 16),
    b: parseInt(hex.slice(5, 7), 16),
  };
}

function rgbToHex({ r, g, b }: { r: number; g: number; b: number }) {
  const toHex = (value: number) => Math.max(0, Math.min(255, Math.round(value))).toString(16).padStart(2, '0');
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

export function mixHex(a: string, b: string, bWeight = 0.5): string {
  const aRgb = hexToRgb(a);
  const bRgb = hexToRgb(b);
  const aWeight = 1 - bWeight;
  return rgbToHex({
    r: aRgb.r * aWeight + bRgb.r * bWeight,
    g: aRgb.g * aWeight + bRgb.g * bWeight,
    b: aRgb.b * aWeight + bRgb.b * bWeight,
  });
}

export function rgbTriplet(hex: string) {
  const { r, g, b } = hexToRgb(hex);
  return `${r}, ${g}, ${b}`;
}

export function withAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}


export function bubbleCue(status: string | undefined): 'none' | 'attention' | 'risk' {
  switch (status) {
    case 'pending_approval':
    case 'needs_input':
    case 'unread_reply':
    case 'done':
      return 'attention';
    case 'failed':
    case 'timeout':
    case 'blocked':
      return 'risk';
    default:
      return 'none';
  }
}


export function toneVars(accent: string, mode: 'dark' | 'light' = 'dark') {
  const core = mode === 'light'
    ? mixHex(accent, LIGHT_BLOB_CORE_COLOR, 0.92)
    : mixHex(accent, BUBBLE_SHELL_COLOR, 0.48);
  const owner = mode === 'light'
    ? mixHex(accent, LIGHT_CONSTELLATION_TEXT_COLOR, 0.9)
    : mixHex(accent, CONSTELLATION_TEXT_COLOR, 0.66);
  return {
    accent,
    accentRgb: rgbTriplet(accent),
    core,
    coreRgb: rgbTriplet(core),
    owner,
    ownerRgb: rgbTriplet(owner),
  };
}


export function interpolateNumber(start: number, end: number, progress: number) {
  return start + (end - start) * progress;
}

function easeInOutSine(progress: number) {
  return -(Math.cos(Math.PI * progress) - 1) / 2;
}

function archiveDotDurationSeconds(
  variant: (typeof ASTRE_ARCHIVE_DOT_PRESETS)[number]['variant'],
  activity: ConstellationActivity,
) {
  const workingDurations = { a: 22, b: 27, c: 32 } as const;
  const idleDurations = { a: 28, b: 34, c: 40 } as const;
  return activity === 'working' ? workingDurations[variant] : idleDurations[variant];
}

export function archiveDotPosition(
  dot: (typeof ASTRE_ARCHIVE_DOT_PRESETS)[number],
  elapsedSeconds: number,
  activity: ConstellationActivity,
  radii: { outerRx: number; outerRy: number; innerRx: number; innerRy: number },
) {
  const duration = archiveDotDurationSeconds(dot.variant, activity);
  const normalizedTime = ((((elapsedSeconds - dot.delay) % duration) + duration) % duration) / duration;
  const segment = normalizedTime * dot.waypoints.length;
  const segmentIndex = Math.floor(segment) % dot.waypoints.length;
  const nextIndex = (segmentIndex + 1) % dot.waypoints.length;
  const eased = easeInOutSine(segment - Math.floor(segment));
  const start = dot.waypoints[segmentIndex];
  const end = dot.waypoints[nextIndex];
  const top = interpolateNumber(start.top, end.top, eased);
  const left = interpolateNumber(start.left, end.left, eased);
  const rx = dot.layer === 'outer' ? radii.outerRx : radii.innerRx;
  const ry = dot.layer === 'outer' ? radii.outerRy : radii.innerRy;

  return {
    x: ((left - 50) / 50) * rx,
    y: ((top - 50) / 50) * ry,
  };
}

export function primitiveTone(_accent: string): ConstellationTone {
  return 'spectral';
}

export function primitiveBlobShape(id: string): ConstellationShape {
  return BLOB_SHAPES[Math.abs(seedHash(String(id))) % BLOB_SHAPES.length] ?? 'alpha';
}

export function primitiveBlobScale(radius: number): ConstellationScale {
  if (radius <= 48) return 'compact';
  return 'standard';
}

export function primitiveBlobTreatment(d: any): ConstellationSignalTreatment {
  if (bubbleCue(d.status) === 'risk') return 'contour';
  return 'bloom';
}

export function primitiveBlobIcon(d: any): ConstellationSignalIcon {
  const attachments = Array.isArray(d.attachments) ? d.attachments : [];
  const attachmentText = attachments
    .map((attachment: any) => `${attachment?.name ?? ''} ${attachment?.filename ?? ''} ${attachment?.content_type ?? ''}`)
    .join(' ');
  const text = `${d.display_title ?? ''} ${d.title ?? ''} ${d.description ?? ''} ${attachmentText}`.toLowerCase();

  if (/\b(api|endpoint|route|webhook|request|response)\b/.test(text)) return 'api';
  if (/\b(db|database|migration|schema|sql|postgres|table)\b/.test(text)) return 'database';
  if (/\b(image|visual|photo|asset|screenshot|storefront|mockup)\b/.test(text)) return 'image';
  if (/\b(design|ui|ux|figma|critique|composer|theme|layout)\b/.test(text)) return 'design';
  if (/\b(test|qa|smoke|check|audit|review)\b/.test(text)) return 'test';
  if (/\b(code|repo|pr|frontend|backend|component|bug)\b/.test(text)) return 'code';
  if (/\b(doc|docs|document|notes|readme|launch)\b/.test(text)) return 'document';
  if (/\b(tool|script|ops|fix|repair)\b/.test(text)) return 'tool';
  return 'thread';
}


export function semanticSummaryText(value: string, maxWords: number, maxChars: number): string {
  const cleaned = String(value || 'Thought').replace(/\s+/g, ' ').trim();
  if (cleaned.length <= maxChars) return cleaned;

  const words = cleaned.split(' ').filter(Boolean);
  const wordSummary = words.slice(0, maxWords).join(' ');
  if (wordSummary.length >= 4 && wordSummary.length <= maxChars) return wordSummary;

  return `${cleaned.slice(0, Math.max(1, maxChars - 1)).trimEnd()}…`;
}
