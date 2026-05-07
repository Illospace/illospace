import {
  buildProjectContextAttachPayload,
  buildProjectContextMessageAttachment,
  type ProjectContextSnapshotLike,
} from '$lib/utils/projectContext';

export function buildWorkspaceComposerAttachmentsWithProjectContext<TAttachment>(
  attachments: readonly TAttachment[],
  projectContext: ProjectContextSnapshotLike | null | undefined,
): Array<TAttachment | ReturnType<typeof buildProjectContextMessageAttachment>> {
  const nextAttachments: Array<TAttachment | ReturnType<typeof buildProjectContextMessageAttachment>> = [
    ...attachments,
  ];
  if (projectContext) {
    nextAttachments.push(buildProjectContextMessageAttachment(projectContext));
  }
  return nextAttachments;
}

export function getWorkspaceComposerProjectContextSaveErrorMessage(err: any): string {
  return err?.detail || 'Thought started, but project context could not be saved.';
}

export async function saveWorkspaceComposerProjectContext(
  ideaId: string,
  projectContext: ProjectContextSnapshotLike | null | undefined,
  attachIdeaProjectContext: (ideaId: string, payload: ReturnType<typeof buildProjectContextAttachPayload>) => Promise<unknown>,
  onError: (err: any) => void,
): Promise<void> {
  if (!projectContext) return;
  try {
    await attachIdeaProjectContext(ideaId, buildProjectContextAttachPayload(projectContext));
  } catch (err: any) {
    onError(err);
  }
}
