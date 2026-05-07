export interface WorkspaceComposerUploadResult<TAttachment> {
  uploaded: TAttachment[];
  failures: unknown[];
}

export async function uploadWorkspaceComposerFiles<TAttachment>(
  files: readonly File[],
  uploadFile: (file: File) => Promise<TAttachment>,
): Promise<WorkspaceComposerUploadResult<Awaited<TAttachment>>> {
  const results = await Promise.allSettled(files.map((file) => uploadFile(file)));
  const uploaded: Awaited<TAttachment>[] = [];
  const failures: unknown[] = [];
  for (const result of results) {
    if (result.status === 'fulfilled') {
      uploaded.push(result.value);
    } else {
      failures.push(result.reason);
    }
  }
  return {
    uploaded,
    failures,
  };
}

export function getWorkspaceComposerUploadFailureMessage(reason: unknown): string {
  return `Upload failed: ${reason instanceof Error ? reason.message : (reason as any)?.message || 'unknown error'}`;
}

export function getWorkspaceComposerPasteFiles(event: ClipboardEvent): File[] {
  const items = event.clipboardData?.items;
  if (!items) return [];
  const files: File[] = [];
  for (const item of items) {
    if (item.kind === 'file') {
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  return files;
}

export function getWorkspaceComposerInputFiles(event: Event): File[] {
  const input = event.target as HTMLInputElement;
  if (!input.files?.length) return [];
  return Array.from(input.files);
}

export function resetWorkspaceComposerFileInput(event: Event): void {
  const input = event.target as HTMLInputElement;
  input.value = '';
}

export function getWorkspaceComposerDropFiles(event: DragEvent): File[] {
  const files = event.dataTransfer?.files;
  if (!files?.length) return [];
  return Array.from(files);
}

export function preventWorkspaceComposerDefaultDrag(event: DragEvent): void {
  event.preventDefault();
}
