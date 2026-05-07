export interface SkillMentionSegment {
  kind: 'text' | 'skill';
  text: string;
  name?: string;
}

const SKILL_MENTION_RE = /(^|\s)\/([A-Za-z0-9_-]+)(?![A-Za-z0-9_-])/g;

export function splitSkillMentions(value: string): SkillMentionSegment[] {
  const segments: SkillMentionSegment[] = [];
  let cursor = 0;

  for (const match of value.matchAll(SKILL_MENTION_RE)) {
    const fullMatch = match[0] ?? '';
    const leading = match[1] ?? '';
    const name = match[2] ?? '';
    const matchStart = match.index ?? 0;
    const mentionStart = matchStart + leading.length;
    const mentionEnd = mentionStart + name.length + 1;

    if (mentionEnd < value.length && value[mentionEnd] === '/') {
      continue;
    }

    if (mentionStart > cursor) {
      segments.push({ kind: 'text', text: value.slice(cursor, mentionStart) });
    }

    segments.push({
      kind: 'skill',
      text: value.slice(mentionStart, mentionEnd),
      name,
    });
    cursor = mentionEnd;

    if (fullMatch.length === 0) {
      break;
    }
  }

  if (cursor < value.length || segments.length === 0) {
    segments.push({ kind: 'text', text: value.slice(cursor) });
  }

  return segments;
}

export function hasSkillMention(value: string): boolean {
  return splitSkillMentions(value).some((segment) => segment.kind === 'skill');
}
