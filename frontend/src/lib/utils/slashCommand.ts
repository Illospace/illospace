export interface SlashCommandToken {
  start: number;
  end: number;
  query: string;
}

function isCommandChar(char: string | undefined): boolean {
  return Boolean(char && /[A-Za-z0-9_-]/.test(char));
}

export function findSlashCommandToken(
  value: string,
  cursorPosition = value.length,
): SlashCommandToken | null {
  const cursor = Math.max(0, Math.min(cursorPosition, value.length));
  let slashIndex = cursor - 1;

  while (slashIndex >= 0 && isCommandChar(value[slashIndex])) {
    slashIndex -= 1;
  }

  if (slashIndex < 0 || value[slashIndex] !== '/') return null;
  if (slashIndex > 0 && !/\s/.test(value[slashIndex - 1])) return null;

  const query = value.slice(slashIndex + 1, cursor);
  if ([...query].some((char) => !isCommandChar(char))) return null;

  let end = cursor;
  while (end < value.length && isCommandChar(value[end])) {
    end += 1;
  }
  if (end < value.length && value[end] === '/') return null;

  return {
    start: slashIndex,
    end,
    query,
  };
}

export function findFirstSlashCommandToken(value: string): SlashCommandToken | null {
  for (let cursor = 0; cursor <= value.length; cursor += 1) {
    if (value[cursor] !== '/') continue;
    const token = findSlashCommandToken(value, cursor + 1);
    if (token) return token;
  }

  return null;
}

export function replaceSlashCommandToken(
  value: string,
  token: SlashCommandToken,
  commandName: string,
): { value: string; cursor: number } {
  const normalizedName = commandName.replace(/^\/+/, '').trim();
  const replacement = `/${normalizedName}`;
  const before = value.slice(0, token.start);
  const after = value.slice(token.end);
  const spacer = after.length === 0 || !/^\s/.test(after) ? ' ' : '';
  const nextValue = `${before}${replacement}${spacer}${after}`;

  return {
    value: nextValue,
    cursor: before.length + replacement.length + spacer.length,
  };
}
