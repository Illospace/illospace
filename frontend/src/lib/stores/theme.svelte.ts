import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';

export type ThemeColorScheme = 'dark' | 'light';
export type ThemeId = 'constellation' | 'daylight';
export type ThemeMode = ThemeColorScheme;

type ThemeDefinition = {
  id: ThemeId;
  label: string;
  colorScheme: ThemeColorScheme;
  icon: ConstellationIconName;
  legacyMode: ThemeMode;
};

export const THEME_STORAGE_KEY = 'illo-theme-id';
const LEGACY_THEME_STORAGE_KEY = 'illo-theme-mode';

export const THEMES = [
  {
    id: 'constellation',
    label: 'Constellation',
    colorScheme: 'dark',
    icon: 'moon',
    legacyMode: 'dark',
  },
  {
    id: 'daylight',
    label: 'Daylight',
    colorScheme: 'light',
    icon: 'sun',
    legacyMode: 'light',
  },
] as const satisfies ReadonlyArray<ThemeDefinition>;

export const THEME_OPTIONS = THEMES.map((theme) => ({
  key: theme.id,
  label: theme.label,
  icon: theme.icon,
}));

const THEME_BY_ID = Object.fromEntries(THEMES.map((theme) => [theme.id, theme])) as Record<
  ThemeId,
  ThemeDefinition
>;

function themeForColorScheme(colorScheme: ThemeColorScheme): ThemeDefinition {
  return THEMES.find((theme) => theme.colorScheme === colorScheme) ?? THEME_BY_ID.constellation;
}

function normalizeThemeId(value: string | null | undefined): ThemeId | null {
  if (value === 'dark') return 'constellation';
  if (value === 'light') return 'daylight';
  return value && value in THEME_BY_ID ? (value as ThemeId) : null;
}

class ThemeStore {
  id = $state<ThemeId>('constellation');
  mode = $state<ThemeMode>('dark');
  hydrated = $state(false);

  init() {
    const nextTheme = this.resolveTheme();
    this.applyTheme(nextTheme);
    this.hydrated = true;
  }

  setTheme(themeId: ThemeId) {
    this.applyTheme(THEME_BY_ID[themeId] ?? THEME_BY_ID.constellation);
    this.persistTheme();
  }

  setMode(mode: ThemeMode) {
    this.applyTheme(themeForColorScheme(mode));
    this.persistTheme();
  }

  private persistTheme() {
    if (typeof localStorage === 'undefined') {
      return;
    }

    try {
      localStorage.setItem(THEME_STORAGE_KEY, this.id);
      localStorage.setItem(LEGACY_THEME_STORAGE_KEY, this.mode);
    } catch {
      // Ignore persistence failures and keep the in-memory theme active.
    }
  }

  private resolveTheme(): ThemeDefinition {
    if (typeof localStorage !== 'undefined') {
      try {
        const stored =
          normalizeThemeId(localStorage.getItem(THEME_STORAGE_KEY)) ??
          normalizeThemeId(localStorage.getItem(LEGACY_THEME_STORAGE_KEY));
        if (stored) {
          return THEME_BY_ID[stored];
        }
      } catch {
        // Ignore storage access failures and fall back to system preference.
      }
    }

    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
      return window.matchMedia('(prefers-color-scheme: light)').matches
        ? THEME_BY_ID.daylight
        : THEME_BY_ID.constellation;
    }

    return THEME_BY_ID[this.id] ?? THEME_BY_ID.constellation;
  }

  private applyTheme(themeDefinition: ThemeDefinition) {
    this.id = themeDefinition.id;
    this.mode = themeDefinition.colorScheme;

    if (typeof document === 'undefined') {
      return;
    }

    document.documentElement.dataset.theme = themeDefinition.id;
    document.documentElement.dataset.colorScheme = themeDefinition.colorScheme;
    document.documentElement.style.colorScheme = themeDefinition.colorScheme;
  }
}

export const theme = new ThemeStore();
