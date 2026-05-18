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
const DROPDOWN_VIEWPORT_GAP = 12;
const DROPDOWN_GAP = 8;
const DROPDOWN_PREFERRED_HEIGHT = 180;
const DROPDOWN_MIN_HEIGHT = 96;
const DROPDOWN_MAX_HEIGHT = 260;
const DROPDOWN_MIN_WIDTH = 220;
const DROPDOWN_MAX_WIDTH = 320;

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

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
  const width = clamp(rect.width, DROPDOWN_MIN_WIDTH, DROPDOWN_MAX_WIDTH);
  const maxLeft = Math.max(DROPDOWN_VIEWPORT_GAP, viewportWidth - width - DROPDOWN_VIEWPORT_GAP);
  const left = clamp(rect.left, DROPDOWN_VIEWPORT_GAP, maxLeft);
  const spaceAbove = Math.max(0, rect.top - DROPDOWN_VIEWPORT_GAP);
  const spaceBelow = Math.max(0, viewportHeight - rect.bottom - DROPDOWN_VIEWPORT_GAP);
  const placement =
    spaceAbove < DROPDOWN_PREFERRED_HEIGHT && spaceBelow > spaceAbove ? 'below' : 'above';
  const availableSpace = placement === 'above' ? spaceAbove : spaceBelow;
  const top = placement === 'above' ? rect.top - DROPDOWN_GAP : rect.bottom + DROPDOWN_GAP;
  const maxHeight = clamp(
    availableSpace - DROPDOWN_GAP,
    DROPDOWN_MIN_HEIGHT,
    DROPDOWN_MAX_HEIGHT,
  );

  return {
    placement,
    style: [
      `--mention-dropdown-left:${left}px`,
      `--mention-dropdown-top:${top}px`,
      `--mention-dropdown-width:${width}px`,
      `--mention-dropdown-max-height:${maxHeight}px`,
    ].join(';'),
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
