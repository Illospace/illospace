export const VOICE_LEVEL_HISTORY_LIMIT = 36;

export function clampVoiceLevel(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

export function appendVoiceLevel(
  history: readonly number[],
  level: number,
  limit = VOICE_LEVEL_HISTORY_LIMIT,
): number[] {
  const safeLimit = Math.max(1, Math.floor(limit));
  const retained = safeLimit > 1 ? history.slice(-(safeLimit - 1)) : [];
  return [...retained, clampVoiceLevel(level)];
}

export function voiceLevelToBarHeight(level: number, minHeight = 3, maxHeight = 24): number {
  const shapedLevel = Math.pow(clampVoiceLevel(level), 0.55);
  return Math.round((minHeight + shapedLevel * (maxHeight - minHeight)) * 10) / 10;
}
