export function loadRunSettings() {
  return import('$lib/features/composer/domain/runSettings');
}

export function loadComposerTextTools() {
  return Promise.all([
    import('$lib/features/composer/components/SlashAutocomplete.svelte'),
    import('$lib/features/composer/components/SkillMentionOverlay.svelte'),
  ]);
}

export function loadVoiceDictation() {
  return Promise.all([
    import('$lib/features/composer/controllers/workspaceVoiceDictation.svelte.ts'),
    import('$lib/features/composer/components/WorkspaceVoiceRecording.svelte'),
  ]);
}

export function loadProjectContextPicker() {
  return import('$lib/features/composer/components/ProjectContextPicker.svelte');
}

export function loadBrowserThoughtPanel() {
  return import('$lib/features/browser-sessions/components/BrowserThoughtPanel.svelte');
}

export function loadThreadCyclesPane() {
  return import('$lib/features/cycles/components/ThreadCyclesPane.svelte');
}

export function loadProjectDraftStatePanel() {
  return import('$lib/features/threads/components/ProjectDraftStatePanel.svelte');
}

export function loadThreadAttachmentPreviewPane() {
  return import('$lib/features/threads/components/ThreadAttachmentPreviewPane.svelte');
}

export function loadThreadCodeReviewPane() {
  return import('$lib/features/threads/components/ThreadCodeReviewPane.svelte');
}

export function loadThreadProjectFilePreviewPane() {
  return import('$lib/features/threads/components/ThreadProjectFilePreviewPane.svelte');
}

export function loadObjectReferencePreviewList() {
  return import('$lib/features/threads/components/ObjectReferencePreviewList.svelte');
}

export function loadStreamVisualBlock() {
  return import('$lib/features/threads/components/StreamVisualBlock.svelte');
}

export function loadThreadAppsPane() {
  return import('$lib/features/workspace-apps/components/ThreadAppsPane.svelte');
}

export function loadVaultPage() {
  return import('../../../../routes/vault/+page.svelte');
}
