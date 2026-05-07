export const CONVERSATION_SCROLL_BOTTOM_THRESHOLD = 100;
export const CONVERSATION_SCROLL_CUE_THRESHOLD = 28;

export function conversationDistanceFromBottom(element: HTMLElement | null | undefined): number {
  if (!element) return 0;
  return Math.max(0, element.scrollHeight - element.scrollTop - element.clientHeight);
}

export function conversationIsNearBottom(
  element: HTMLElement | null | undefined,
  threshold = CONVERSATION_SCROLL_BOTTOM_THRESHOLD,
): boolean {
  return conversationDistanceFromBottom(element) <= threshold;
}

export function shouldShowConversationScrollCue(
  element: HTMLElement | null | undefined,
  threshold = CONVERSATION_SCROLL_CUE_THRESHOLD,
): boolean {
  return conversationDistanceFromBottom(element) > threshold;
}

export function scrollConversationToBottom(element: HTMLElement | null | undefined): void {
  if (!element) return;
  element.scrollTo({ top: element.scrollHeight });
}
