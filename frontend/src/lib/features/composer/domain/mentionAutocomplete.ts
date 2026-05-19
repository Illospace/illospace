import {
  anchoredShortcutMenuGeometry,
  shortcutMenuCssVariables,
} from '$lib/features/composer/domain/shortcutMenu';

export type MentionAutocompleteOption = {
  id?: string;
  name: string;
  insertText?: string;
  color?: string | null;
  isIllo?: boolean;
  hint?: string;
  keywords?: string[];
};

export type MentionDropdownPlacement = 'above' | 'below';

export type MentionDropdownGeometry = {
  placement: MentionDropdownPlacement;
  style: string;
};

const MENTION_HANDLE_SPLIT_RE = /[._+\-\s]+/;
const MENTION_HANDLE_INVALID_RE = /[^a-z0-9._-]+/g;
const DROPDOWN_PREFERRED_HEIGHT = 180;
const DROPDOWN_MIN_HEIGHT = 96;
const DROPDOWN_MAX_HEIGHT = 260;
const DROPDOWN_MIN_WIDTH = 220;
const DROPDOWN_MAX_WIDTH = 320;

export function normalizeMentionHandle(value: string | null | undefined) {
  return (value ?? '')
    .trim()
    .toLowerCase()
    .replace(/^@+/, '')
    .replace(/\s+/g, '')
    .replace(MENTION_HANDLE_INVALID_RE, '');
}

export function mentionHandleForPerson(person: { name?: string | null; email?: string | null }) {
  const emailLocal = (person.email ?? '').split('@', 1)[0].trim().toLowerCase();
  const emailHandle = normalizeMentionHandle(emailLocal);
  if (emailHandle) return emailHandle;

  const nameParts = (person.name ?? '').trim().toLowerCase().split(MENTION_HANDLE_SPLIT_RE);
  const nameHandle = normalizeMentionHandle(nameParts.join(''));
  if (nameHandle) return nameHandle;

  return '';
}

export function mentionDropdownGeometry(
  rect: DOMRect,
  viewportWidth: number,
  viewportHeight: number,
): MentionDropdownGeometry {
  const geometry = anchoredShortcutMenuGeometry(rect, viewportWidth, viewportHeight, {
    preferredHeight: DROPDOWN_PREFERRED_HEIGHT,
    minHeight: DROPDOWN_MIN_HEIGHT,
    maxHeight: DROPDOWN_MAX_HEIGHT,
    minWidth: DROPDOWN_MIN_WIDTH,
    maxWidth: DROPDOWN_MAX_WIDTH,
  });

  return {
    placement: geometry.placement,
    style: shortcutMenuCssVariables(geometry, 'mention'),
  };
}

export function defaultIlloMentionOption(): MentionAutocompleteOption {
  return {
    id: 'illo',
    name: 'Illo',
    insertText: 'illo',
    color: '#5ea898',
    isIllo: true,
    hint: 'Mention Illo',
    keywords: ['illo', 'ai', 'assistant'],
  };
}
