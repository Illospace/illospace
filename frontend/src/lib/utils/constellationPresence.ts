const COMPOSER_ACTION_SHELL = '#050910';
const COMPOSER_ACTION_OWNER = '#f0f0fa';

export function normalizeHexColor(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(trimmed)) return null;
  if (trimmed.length === 4) {
    return `#${trimmed[1]}${trimmed[1]}${trimmed[2]}${trimmed[2]}${trimmed[3]}${trimmed[3]}`;
  }
  return trimmed;
}

export function mixHex(a: string, b: string, bWeight = 0.5): string {
  const hexA = normalizeHexColor(a);
  const hexB = normalizeHexColor(b);
  if (!hexA || !hexB) return hexA ?? hexB ?? '#000000';

  const aWeight = 1 - bWeight;
  const toRgb = (hex: string) => [
    Number.parseInt(hex.slice(1, 3), 16),
    Number.parseInt(hex.slice(3, 5), 16),
    Number.parseInt(hex.slice(5, 7), 16),
  ] as const;

  const [ar, ag, ab] = toRgb(hexA);
  const [br, bg, bb] = toRgb(hexB);
  const blend = (left: number, right: number) =>
    Math.round(left * aWeight + right * bWeight)
      .toString(16)
      .padStart(2, '0');

  return `#${blend(ar, br)}${blend(ag, bg)}${blend(ab, bb)}`;
}

export function buildPresenceSeedStyle(color: string | null | undefined): string {
  const accent = normalizeHexColor(color);
  if (!accent) return '';

  return [
    `--seed-accent:${accent}`,
    `--seed-core:color-mix(in srgb, ${accent} var(--constellation-presence-seed-user-core-accent-strength, 52%), var(--constellation-presence-seed-user-core-base, #050910))`,
    `--seed-owner:color-mix(in srgb, ${accent} var(--constellation-presence-seed-user-owner-accent-strength, 18%), var(--constellation-presence-seed-user-owner-base, #f0f0fa))`,
  ].join('; ');
}

export function presenceToneForColor(_color: string | null | undefined): 'spectral' | 'amber' {
  return 'spectral';
}

export function buildComposerActionStyle(color: string | null | undefined): string {
  const accent = normalizeHexColor(color);
  if (!accent) return '';

  const core = mixHex(accent, COMPOSER_ACTION_SHELL, 0.68);
  const owner = mixHex(accent, COMPOSER_ACTION_OWNER, 0.78);

  return [
    `--constellation-composer-action-accent:${accent}`,
    `--constellation-composer-action-core:${core}`,
    `--constellation-composer-action-owner:${owner}`,
  ].join('; ');
}
