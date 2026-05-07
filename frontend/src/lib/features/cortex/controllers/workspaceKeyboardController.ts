export interface WorkspaceKeyboardState {
  chatDockExpanded: boolean;
  canvasOpen: boolean;
  activeWorkspaceAppId: string | null;
  opsOpen: boolean;
  timelineOpen: boolean;
  panelOpen: boolean;
}

export interface WorkspaceKeyboardActions {
  compactChat(): void;
  closeChat(): void;
  openChat(): void;
  closeCanvas(): void;
  closeWorkspaceApp(): void;
  closeOps(): void;
  closeTimeline(): void;
  closeThread(): void;
  toggleTimeline(): void;
  toggleOps(): void;
  setConstellationMode(active: boolean): void;
}

export function isEditableTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false;

  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

export function releaseWorkspaceShortcutFocus() {
  if (typeof document === 'undefined') return;

  const blurActiveElement = () => {
    const activeElement = document.activeElement;
    if (!(activeElement instanceof HTMLElement) || isEditableTarget(activeElement)) return;
    activeElement.blur();
  };

  blurActiveElement();
  requestAnimationFrame(blurActiveElement);
}

export function dispatchWorkspaceSceneCommand(command: 'fit-view' | 'toggle-pause') {
  if (typeof window === 'undefined') return;
  const eventName = command === 'fit-view'
    ? 'cortex-fit-view'
    : 'cortex-toggle-pause';
  window.dispatchEvent(new CustomEvent(eventName));
}

export function handleWorkspaceKeydown(
  event: KeyboardEvent,
  state: WorkspaceKeyboardState,
  actions: WorkspaceKeyboardActions,
) {
  if (isEditableTarget(event.target)) return;

  if (event.key === 'Tab' && !event.shiftKey && !event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault();
    if (state.chatDockExpanded) {
      actions.compactChat();
    } else {
      actions.openChat();
    }
    releaseWorkspaceShortcutFocus();
  } else if (event.key === 'Escape') {
    let handled = true;

    if (state.chatDockExpanded) {
      actions.closeChat();
    } else if (state.canvasOpen) {
      actions.closeCanvas();
    } else if (state.activeWorkspaceAppId) {
      actions.closeWorkspaceApp();
    } else if (state.opsOpen) {
      actions.closeOps();
    } else if (state.timelineOpen) {
      actions.closeTimeline();
    } else if (state.panelOpen) {
      actions.closeThread();
    } else {
      handled = false;
    }

    if (handled) {
      event.preventDefault();
      releaseWorkspaceShortcutFocus();
    }
  } else if (event.key === 't' || event.key === 'T') {
    actions.toggleTimeline();
  } else if (event.key === 'o' || event.key === 'O') {
    actions.toggleOps();
  } else if (event.key === 'Shift' && !event.repeat) {
    actions.setConstellationMode(true);
  } else if (event.key === 'f' || event.key === 'F') {
    dispatchWorkspaceSceneCommand('fit-view');
  } else if (event.key === ' ') {
    event.preventDefault();
    dispatchWorkspaceSceneCommand('toggle-pause');
  }
}

export function handleWorkspaceKeyup(
  event: KeyboardEvent,
  actions: Pick<WorkspaceKeyboardActions, 'setConstellationMode'>,
) {
  if (event.key === 'Shift') {
    actions.setConstellationMode(false);
  }
}
