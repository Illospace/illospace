import type { ConstellationActivity } from './constellationTypes';
import { normalizeHexColor } from '$lib/utils/constellationPresence';

type AstreStyleMode = 'dark' | 'light';

const LIGHT_ASTRE_USER_PALETTE = [
  '#c51f4a',
  '#c026d3',
  '#6d28d9',
  '#4c1d95',
  '#087f5b',
  '#166534',
  '#6f8f00',
  '#9a7b00',
] as const;

export type AstrePrimitiveStyleOptions = {
  id: string;
  accent: string | null | undefined;
  mode: AstreStyleMode;
  activity?: ConstellationActivity;
  x?: number;
  y?: number;
  size?: number;
  opacity?: number;
  zIndex?: number | string;
  emphasis?: boolean;
};

function styleDeclarations(declarations: Record<string, string | number | null | undefined>) {
  return Object.entries(declarations)
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(([key, value]) => `${key}: ${value}`)
    .join('; ');
}

function seedHash(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = ((hash << 5) - hash + value.charCodeAt(index)) | 0;
  }
  return hash;
}

function colorAlpha(color: string, alpha: number) {
  const percentage = Math.max(0, Math.min(100, alpha * 100));
  return `color-mix(in srgb, ${color} ${percentage}%, transparent)`;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function hexToRgb(hex: string) {
  return {
    r: Number.parseInt(hex.slice(1, 3), 16) / 255,
    g: Number.parseInt(hex.slice(3, 5), 16) / 255,
    b: Number.parseInt(hex.slice(5, 7), 16) / 255,
  };
}

function rgbToHex({ r, g, b }: { r: number; g: number; b: number }) {
  const toHex = (value: number) => Math.round(clamp(value, 0, 1) * 255).toString(16).padStart(2, '0');
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

function rgbToHsl({ r, g, b }: { r: number; g: number; b: number }) {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const lightness = (max + min) / 2;
  const delta = max - min;

  if (delta === 0) {
    return { h: 0, s: 0, l: lightness };
  }

  const saturation = delta / (1 - Math.abs(2 * lightness - 1));
  let hue = 0;

  if (max === r) {
    hue = ((g - b) / delta) % 6;
  } else if (max === g) {
    hue = (b - r) / delta + 2;
  } else {
    hue = (r - g) / delta + 4;
  }

  return { h: (hue * 60 + 360) % 360, s: saturation, l: lightness };
}

function hslToRgb({ h, s, l }: { h: number; s: number; l: number }) {
  const chroma = (1 - Math.abs(2 * l - 1)) * s;
  const hue = h / 60;
  const x = chroma * (1 - Math.abs((hue % 2) - 1));
  let r = 0;
  let g = 0;
  let b = 0;

  if (hue < 1) {
    r = chroma;
    g = x;
  } else if (hue < 2) {
    r = x;
    g = chroma;
  } else if (hue < 3) {
    g = chroma;
    b = x;
  } else if (hue < 4) {
    g = x;
    b = chroma;
  } else if (hue < 5) {
    r = x;
    b = chroma;
  } else {
    r = chroma;
    b = x;
  }

  const match = l - chroma / 2;
  return { r: r + match, g: g + match, b: b + match };
}

function resolveLightAstreAccent(accent: string | null, seed: number) {
  if (!accent) return LIGHT_ASTRE_USER_PALETTE[seed % LIGHT_ASTRE_USER_PALETTE.length];

  const { h, s, l } = rgbToHsl(hexToRgb(accent));
  const isWaterRange = h >= 164 && h <= 240;
  const isEarthRange = h >= 10 && h <= 48;
  const isVerySoft = s < 0.42 || l > 0.62;

  if (isEarthRange || isWaterRange || isVerySoft) {
    return LIGHT_ASTRE_USER_PALETTE[(seed + 2) % LIGHT_ASTRE_USER_PALETTE.length];
  }

  return rgbToHex(
    hslToRgb({
      h,
      s: clamp(Math.max(s * 1.22, 0.62), 0.58, 0.84),
      l: clamp(l, 0.38, 0.5),
    }),
  );
}

export function buildAstrePrimitiveStyle({
  id,
  accent,
  mode,
  activity = 'idle',
  x,
  y,
  size,
  opacity,
  zIndex,
  emphasis = false,
}: AstrePrimitiveStyleOptions) {
  const isLightMode = mode === 'light';
  const astreSeed = Math.abs(seedHash(String(id))) || 1;
  const sourceAccent = normalizeHexColor(accent);
  const normalizedAccent = isLightMode
    ? resolveLightAstreAccent(sourceAccent, astreSeed)
    : sourceAccent ?? 'var(--constellation-color-spectral)';
  const highlightX = 34 + (astreSeed % 19);
  const highlightY = 22 + (Math.floor(astreSeed / 7) % 18);
  const diffuseX = 56 + (Math.floor(astreSeed / 13) % 18);
  const diffuseY = 62 + (Math.floor(astreSeed / 19) % 17);
  const owner = isLightMode
    ? `color-mix(in srgb, ${normalizedAccent} 24%, var(--constellation-color-text-primary) 76%)`
    : `color-mix(in srgb, ${normalizedAccent} 28%, var(--constellation-color-text-primary) 72%)`;
  const coreBackground = isLightMode
    ? [
        `radial-gradient(circle at ${highlightX}% ${highlightY}%, color-mix(in srgb, white 70%, ${normalizedAccent} 30%) 0%, transparent 25%)`,
        `radial-gradient(circle at ${diffuseX}% ${diffuseY}%, color-mix(in srgb, ${normalizedAccent} 40%, transparent) 0%, transparent 62%)`,
        `radial-gradient(circle at 50% 54%, color-mix(in srgb, ${normalizedAccent} 24%, var(--bg-3)) 0%, color-mix(in srgb, ${normalizedAccent} 36%, var(--bg-2)) 78%)`,
      ].join(', ')
    : [
        `radial-gradient(circle at ${highlightX}% ${highlightY}%, color-mix(in srgb, ${normalizedAccent} 12%, rgba(255, 242, 218, 0.045)) 0%, transparent 18%)`,
        `radial-gradient(circle at ${diffuseX}% ${diffuseY}%, color-mix(in srgb, ${normalizedAccent} 17%, transparent) 0%, transparent 52%)`,
        `radial-gradient(circle at 50% 54%, color-mix(in srgb, ${normalizedAccent} 22%, var(--bg-2)) 0%, color-mix(in srgb, ${normalizedAccent} 12%, var(--bg-0)) 78%)`,
      ].join(', ');
  const lightBaseHaloRest = activity === 'working' ? 0.52 : 0.42;
  const lightBaseHaloPulseMin = activity === 'working' ? 0.4 : 0.3;
  const lightBaseHaloPulseMax = activity === 'working' ? 0.64 : 0.54;
  const lightBaseRingShadow = `0 0 0 1px color-mix(in srgb, ${normalizedAccent} 40%, white 60%), 0 16px 34px color-mix(in srgb, var(--constellation-color-text-primary) 10%, transparent), 0 0 30px ${colorAlpha(normalizedAccent, 0.35)}, 0 0 66px ${colorAlpha(normalizedAccent, 0.18)}`;
  const lightBaseCoreShadow = [
    `inset 0 0 0 1px var(--astre-core-inner-stroke)`,
    `inset 0 16px 30px color-mix(in srgb, white 32%, ${normalizedAccent} 14%)`,
    `inset 0 -22px 32px color-mix(in srgb, ${normalizedAccent} 16%, rgba(26, 39, 49, 0.08))`,
    `0 10px 24px color-mix(in srgb, ${normalizedAccent} 18%, transparent)`,
    `0 0 56px ${colorAlpha(normalizedAccent, 0.18)}`,
  ].join(', ');
  const lightLitRingShadow = [
    `0 0 0 1px color-mix(in srgb, white 72%, transparent)`,
    `0 12px 30px color-mix(in srgb, ${normalizedAccent} 14%, transparent)`,
    `0 0 46px color-mix(in srgb, ${normalizedAccent} 12%, transparent)`,
  ].join(', ');
  const lightLitCoreShadow = [
    `inset 0 0 0 1px var(--astre-core-inner-stroke)`,
    `inset 0 18px 30px color-mix(in srgb, white 46%, ${normalizedAccent} 4%)`,
    `inset 0 -24px 34px color-mix(in srgb, ${normalizedAccent} 10%, rgba(156, 109, 36, 0.1))`,
    `0 12px 30px color-mix(in srgb, ${normalizedAccent} 14%, transparent)`,
    `0 0 54px color-mix(in srgb, var(--constellation-color-amber-core) 12%, transparent)`,
  ].join(', ');
  const lightUseBrightHover = isLightMode && emphasis;
  const lightUseCalmRest = isLightMode && !emphasis;
  const lightRingBorder = `color-mix(in srgb, ${normalizedAccent} 48%, rgba(255, 253, 247, 0.52))`;
  const lightLitRingBorder = `color-mix(in srgb, ${normalizedAccent} 50%, rgba(255, 249, 238, 0.5))`;

  return styleDeclarations({
    left: Number.isFinite(x) ? `${x}px` : undefined,
    top: Number.isFinite(y) ? `${y}px` : undefined,
    width: Number.isFinite(size) ? `${size}px` : undefined,
    height: Number.isFinite(size) ? `${size}px` : undefined,
    opacity,
    'z-index': zIndex,
    '--astre-scale': emphasis ? 1.1 : 1,
    '--astre-tone-color': normalizedAccent,
    '--astre-diffuse-x': `${highlightX}%`,
    '--astre-diffuse-y': `${highlightY}%`,
    '--astre-before-border': colorAlpha(normalizedAccent, isLightMode ? 0.24 : 0.26),
    '--astre-halo-border': colorAlpha(normalizedAccent, isLightMode ? 0.26 : 0.34),
    '--astre-halo-inner-border': colorAlpha(normalizedAccent, isLightMode ? 0.1 : 0.14),
    '--astre-outer-ring-opacity': emphasis ? 0.46 : lightUseCalmRest ? 0.32 : undefined,
    '--astre-halo-rest-opacity': isLightMode
      ? lightUseBrightHover ? 0.92 : lightBaseHaloRest
      : emphasis ? 0.82 : activity === 'working' ? 0.72 : 0.58,
    '--astre-halo-inner-opacity': emphasis ? (isLightMode ? 0.42 : 0.34) : lightUseCalmRest ? 0 : undefined,
    '--astre-halo-pulse-min': isLightMode
      ? lightUseBrightHover ? 0.72 : lightBaseHaloPulseMin
      : emphasis ? 0.64 : activity === 'working' ? 0.58 : 0.44,
    '--astre-halo-pulse-max': isLightMode
      ? lightUseBrightHover ? 1 : lightBaseHaloPulseMax
      : emphasis ? 0.92 : activity === 'working' ? 0.84 : 0.68,
    '--astre-ring-opacity': emphasis ? 1 : isLightMode ? 0.78 : 0.88,
    '--astre-ring-filter': lightUseBrightHover
      ? 'saturate(1.34) brightness(1.12)'
      : lightUseCalmRest ? 'saturate(1.16) brightness(1.02)' : undefined,
    '--astre-ring-border': isLightMode
      ? lightUseBrightHover ? lightLitRingBorder : lightRingBorder
      : undefined,
    '--astre-ring-shadow': isLightMode
      ? lightUseBrightHover ? lightLitRingShadow : lightBaseRingShadow
      : `0 0 24px ${colorAlpha(normalizedAccent, 0.38)}, 0 0 70px ${colorAlpha(normalizedAccent, 0.18)}, 0 0 112px ${colorAlpha(normalizedAccent, 0.08)}`,
    '--astre-core-background': coreBackground,
    '--astre-core-color': owner,
    '--astre-core-border-color': isLightMode
      ? `color-mix(in srgb, ${normalizedAccent} 42%, rgba(255, 255, 255, 0.64))`
      : `color-mix(in srgb, ${normalizedAccent} 44%, var(--constellation-surface-panel-border))`,
    '--astre-core-inner-stroke': isLightMode
      ? `color-mix(in srgb, ${normalizedAccent} 20%, color-mix(in srgb, white 70%, transparent))`
      : `color-mix(in srgb, ${normalizedAccent} 16%, color-mix(in srgb, white 20%, transparent))`,
    '--astre-core-glow-strong': colorAlpha(normalizedAccent, isLightMode ? 0.32 : 0.48),
    '--astre-core-glow-soft': colorAlpha(normalizedAccent, isLightMode ? 0.2 : 0.26),
    '--constellation-astre-core-sheen-background': isLightMode
      ? [
          `radial-gradient(circle at ${highlightX}% ${highlightY}%, color-mix(in srgb, white 72%, ${normalizedAccent} 28%) 0%, transparent 23%)`,
          `linear-gradient(180deg, color-mix(in srgb, white 28%, ${normalizedAccent} 10%), transparent 42%)`,
        ].join(', ')
      : undefined,
    '--constellation-astre-core-sheen-opacity': isLightMode ? 0.54 : undefined,
    '--constellation-astre-core-contour-shadow': isLightMode
      ? [
          `inset 0 0 22px rgba(255, 255, 255, 0.16)`,
          `inset 0 -18px 28px color-mix(in srgb, ${normalizedAccent} 5%, rgba(26, 39, 49, 0.04))`,
        ].join(', ')
      : undefined,
    '--constellation-astre-core-contour-filter': isLightMode
      ? `drop-shadow(0 0 3px color-mix(in srgb, ${normalizedAccent} 8%, transparent))`
      : undefined,
    '--constellation-astre-owner-color': isLightMode
      ? `color-mix(in srgb, ${normalizedAccent} 18%, rgba(18, 27, 36, 0.98) 82%)`
      : undefined,
    '--constellation-astre-owner-shadow': isLightMode
      ? `0 0 9px color-mix(in srgb, ${normalizedAccent} 28%, rgba(255, 255, 255, 0.46))`
      : undefined,
    '--astre-core-transform': lightUseBrightHover ? 'scale(1.05)' : lightUseCalmRest ? 'none' : undefined,
    '--astre-core-filter': lightUseBrightHover
      ? 'saturate(1.2) brightness(1.06)'
      : lightUseCalmRest ? 'saturate(1.12) brightness(1.01)' : undefined,
    '--astre-core-shadow': isLightMode
      ? lightUseBrightHover ? lightLitCoreShadow : lightBaseCoreShadow
      : undefined,
    '--astre-archive-opacity': lightUseBrightHover ? 1 : isLightMode ? 0.96 : undefined,
    '--astre-archive-filter': lightUseBrightHover
      ? 'saturate(1.32) brightness(1.14)'
      : lightUseCalmRest ? 'saturate(1.26) brightness(1.04)' : undefined,
    '--astre-emphasis-ring-filter': isLightMode
      ? 'saturate(1.34) brightness(1.16)'
      : 'saturate(1.3) brightness(1.16)',
    '--astre-emphasis-archive-filter': isLightMode
      ? 'saturate(1.28) brightness(1.14)'
      : 'saturate(1.24) brightness(1.16)',
    '--astre-emphasis-core-filter': 'saturate(1.28) brightness(1.08)',
    '--astre-emphasis-core-transform': 'scale(1.05)',
    '--astre-emphasis-ring-border': isLightMode
      ? lightLitRingBorder
      : `color-mix(in srgb, var(--astre-rim-hot) 58%, rgba(255, 244, 220, 0.18))`,
    '--astre-emphasis-ring-shadow': isLightMode
      ? lightLitRingShadow
      : [
          `0 0 20px color-mix(in srgb, ${normalizedAccent} 36%, transparent)`,
          `0 0 58px color-mix(in srgb, ${normalizedAccent} 18%, transparent)`,
        ].join(', '),
    '--astre-emphasis-core-shadow': isLightMode
      ? lightLitCoreShadow
      : [
          `inset 0 0 0 1px var(--astre-core-inner-stroke)`,
          `inset 0 18px 32px rgba(255, 236, 200, 0.026)`,
          `inset 0 -38px 52px rgba(0, 0, 0, 0.38)`,
          `0 0 34px ${colorAlpha(normalizedAccent, 0.48)}`,
          `0 0 86px ${colorAlpha(normalizedAccent, 0.26)}`,
        ].join(', '),
    '--astre-rim-hot': isLightMode
      ? `color-mix(in srgb, ${normalizedAccent} 50%, rgba(255, 253, 247, 0.5))`
      : `color-mix(in srgb, ${normalizedAccent} 52%, var(--constellation-color-amber-owner) 48%)`,
    '--astre-rim-soft': isLightMode
      ? `color-mix(in srgb, ${normalizedAccent} 28%, rgba(255, 253, 247, 0.5))`
      : `color-mix(in srgb, ${normalizedAccent} 34%, var(--constellation-color-amber-owner))`,
    '--astre-rim-dim': colorAlpha(normalizedAccent, isLightMode ? 0.12 : 0.18),
    '--astre-rim-opacity': isLightMode ? (lightUseBrightHover ? 0.74 : 0.5) : 1,
  });
}
