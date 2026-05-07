export interface RunDecision {
  shouldRun: boolean;
  isExplicit: boolean;
  reason: 'message' | 'slash_command' | 'none';
}

function isCommandChar(char: string | undefined): boolean {
  if (!char) return false;
  const code = char.charCodeAt(0);
  return (
    (code >= 48 && code <= 57) ||
    (code >= 65 && code <= 90) ||
    (code >= 97 && code <= 122) ||
    char === '_' ||
    char === '-'
  );
}

function isWhitespace(char: string | undefined): boolean {
  return char === ' ' || char === '\n' || char === '\r' || char === '\t';
}

function hasSlashCommandToken(text: string): boolean {
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] !== '/') continue;
    if (index > 0 && !isWhitespace(text[index - 1])) continue;

    let end = index + 1;
    if (!isCommandChar(text[end])) continue;
    while (end < text.length && isCommandChar(text[end])) {
      end += 1;
    }
    if (end < text.length && text[end] === '/') continue;
    return true;
  }
  return false;
}

export function getRunDecision(content: string): RunDecision {
  const text = content.trim();
  if (!text) return { shouldRun: false, isExplicit: false, reason: 'none' };
  if (hasSlashCommandToken(text)) {
    return { shouldRun: true, isExplicit: true, reason: 'slash_command' };
  }
  return { shouldRun: true, isExplicit: true, reason: 'message' };
}

export function getRunHint(content: string, attachmentCount = 0): string {
  const text = content.trim();
  if (!text) return attachmentCount > 0 ? 'Attachment ready' : '';
  const decision = getRunDecision(text);
  if (decision.reason === 'slash_command') return 'Skill command';
  if (decision.shouldRun) return 'Illo will respond';
  return '';
}
