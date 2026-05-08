import type { ProjectContextResource } from './projectContext';

export type DroppedFile = File & { webkitRelativePath?: string };
export type DroppedEntryFile = { file: File; relativePath: string };
export type FileSystemEntryLike = {
  name: string;
  isFile: boolean;
  isDirectory: boolean;
};
export type FileSystemFileEntryLike = FileSystemEntryLike & {
  file: (
    successCallback: (file: File) => void,
    errorCallback?: (error: DOMException) => void,
  ) => void;
};
export type FileSystemDirectoryReaderLike = {
  readEntries: (
    successCallback: (entries: FileSystemEntryLike[]) => void,
    errorCallback?: (error: DOMException) => void,
  ) => void;
};
export type FileSystemDirectoryEntryLike = FileSystemEntryLike & {
  createReader: () => FileSystemDirectoryReaderLike;
};
export type DataTransferItemWithEntry = {
  webkitGetAsEntry?: () => FileSystemEntryLike | null;
};

export type ProjectContextUploadedFile = {
  upload_id?: string;
  filename: string;
  relative_path: string;
  storage_path: string;
  uri: string;
  mime?: string;
  size?: number;
};

export type ProjectContextUploadSkip = {
  filename?: string;
  reason?: string;
};

export type ProjectContextUploadFilterResult = {
  entries: DroppedEntryFile[];
  skippedFiles: ProjectContextUploadSkip[];
};

export const PROJECT_CONTEXT_UPLOAD_MAX_FILES = 200;
export const PROJECT_CONTEXT_UPLOAD_MAX_FILE_SIZE = 10_000_000;
export const PROJECT_CONTEXT_UPLOAD_MAX_TOTAL_SIZE = 20_000_000;
const PROJECT_CONTEXT_ENTRY_READ_TIMEOUT_MS = 8_000;

export function enableFolderPicker(node: HTMLInputElement) {
  const folderNode = node as HTMLInputElement & { webkitdirectory?: boolean; directory?: boolean };
  folderNode.webkitdirectory = true;
  folderNode.directory = true;
  node.setAttribute('webkitdirectory', '');
  node.setAttribute('directory', '');
}

export function relativePathForFile(file: File): string {
  return (file as DroppedFile).webkitRelativePath || file.name;
}

export function uploadSkippedSummary(skippedFiles: Array<{ filename?: string; reason?: string }> | undefined): string {
  if (!skippedFiles?.length) return '';
  const first = skippedFiles[0];
  const suffix = skippedFiles.length > 1 ? ` (${skippedFiles.length} skipped)` : '';
  return `${first.filename ?? 'Some files'}: ${first.reason ?? 'Skipped'}${suffix}`;
}

function timeoutError(label: string): Error {
  return new Error(`${label} took too long to read.`);
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(timeoutError(label)), timeoutMs);
    promise.then(
      (value) => {
        clearTimeout(timeout);
        resolve(value);
      },
      (error) => {
        clearTimeout(timeout);
        reject(error);
      },
    );
  });
}

export function filterProjectContextUploadEntries(entries: DroppedEntryFile[]): ProjectContextUploadFilterResult {
  const accepted: DroppedEntryFile[] = [];
  const skippedFiles: ProjectContextUploadSkip[] = [];
  let totalSize = 0;

  for (const entry of entries) {
    const filename = entry.relativePath || entry.file.name || 'file';
    const size = typeof entry.file.size === 'number' ? entry.file.size : 0;

    if (accepted.length >= PROJECT_CONTEXT_UPLOAD_MAX_FILES) {
      skippedFiles.push({
        filename,
        reason: `Only the first ${PROJECT_CONTEXT_UPLOAD_MAX_FILES} supported files are attached`,
      });
      continue;
    }
    if (size > PROJECT_CONTEXT_UPLOAD_MAX_FILE_SIZE) {
      skippedFiles.push({
        filename,
        reason: `File is larger than ${PROJECT_CONTEXT_UPLOAD_MAX_FILE_SIZE / 1_000_000} MB`,
      });
      continue;
    }
    if (totalSize + size > PROJECT_CONTEXT_UPLOAD_MAX_TOTAL_SIZE) {
      skippedFiles.push({
        filename,
        reason: `Project Context upload is capped at ${PROJECT_CONTEXT_UPLOAD_MAX_TOTAL_SIZE / 1_000_000} MB`,
      });
      continue;
    }

    accepted.push(entry);
    totalSize += size;
  }

  return { entries: accepted, skippedFiles };
}

export function uploadedFileResource(file: ProjectContextUploadedFile, source: string): ProjectContextResource {
  return {
    type: 'file',
    kind: 'file',
    label: file.relative_path || file.filename,
    name: file.filename,
    path: file.storage_path,
    uri: file.uri,
    source,
    size: file.size,
    mime: file.mime,
    access: 'read',
    allowed_paths: [file.storage_path],
    uploaded_files: [file],
    uploaded_file_count: 1,
  };
}

export function uploadedFolderResources(
  files: ProjectContextUploadedFile[],
  source: string,
): ProjectContextResource[] {
  const groups = new Map<string, ProjectContextUploadedFile[]>();
  for (const file of files) {
    const [root] = (file.relative_path || file.filename).split('/');
    const groupName = root || 'Uploaded files';
    groups.set(groupName, [...(groups.get(groupName) ?? []), file]);
  }
  return Array.from(groups.entries()).map(([name, group]) => ({
    type: 'folder',
    kind: 'folder',
    label: name,
    name,
    uri: `project-context-upload://${encodeURIComponent(name)}${group[0]?.upload_id ? `?upload=${encodeURIComponent(group[0].upload_id)}` : ''}`,
    source,
    file_manifest: group.map((file) => file.relative_path).slice(0, 200),
    file_count: group.length,
    uploaded_file_count: group.length,
    uploaded_files: group,
    allowed_paths: group.map((file) => file.storage_path),
    access: 'read',
  }));
}

export function readFileEntry(entry: FileSystemFileEntryLike, relativePath: string): Promise<DroppedEntryFile> {
  return withTimeout(
    new Promise((resolve, reject) => {
      entry.file(
        (file) => resolve({ file, relativePath }),
        (error) => reject(error),
      );
    }),
    PROJECT_CONTEXT_ENTRY_READ_TIMEOUT_MS,
    relativePath,
  );
}

export function readDirectoryEntries(reader: FileSystemDirectoryReaderLike): Promise<FileSystemEntryLike[]> {
  return withTimeout(
    new Promise((resolve, reject) => {
      const entries: FileSystemEntryLike[] = [];
      const readBatch = () => {
        reader.readEntries(
          (batch) => {
            if (!batch.length) {
              resolve(entries);
              return;
            }
            entries.push(...batch);
            readBatch();
          },
          (error) => reject(error),
        );
      };
      readBatch();
    }),
    PROJECT_CONTEXT_ENTRY_READ_TIMEOUT_MS,
    'Folder',
  );
}

export async function readDroppedEntry(entry: FileSystemEntryLike, parentPath = ''): Promise<DroppedEntryFile[]> {
  const relativePath = parentPath ? `${parentPath}/${entry.name}` : entry.name;
  if (entry.isFile) {
    return [await readFileEntry(entry as FileSystemFileEntryLike, relativePath)];
  }
  if (!entry.isDirectory) return [];
  const reader = (entry as FileSystemDirectoryEntryLike).createReader();
  const entries = await readDirectoryEntries(reader);
  const groups = await Promise.all(entries.map((item) => readDroppedEntry(item, relativePath)));
  return groups.flat();
}

export function entriesFromFileList(files: File[]): DroppedEntryFile[] {
  return files.map((file) => ({ file, relativePath: relativePathForFile(file) }));
}

export async function entriesFromDataTransfer(transfer: DataTransfer): Promise<DroppedEntryFile[]> {
  const fallbackFiles = Array.from(transfer.files ?? []);
  const fileSystemEntries = Array.from(transfer.items ?? []).reduce<FileSystemEntryLike[]>((acc, item) => {
    const entry = (item as unknown as DataTransferItemWithEntry).webkitGetAsEntry?.();
    if (entry) acc.push(entry);
    return acc;
  }, []);

  if (!fileSystemEntries.length) {
    return entriesFromFileList(fallbackFiles);
  }

  try {
    const entryGroups = await Promise.all(fileSystemEntries.map((entry) => readDroppedEntry(entry)));
    const entries = entryGroups.flat();
    return entries.length ? entries : entriesFromFileList(fallbackFiles);
  } catch {
    return entriesFromFileList(fallbackFiles);
  }
}
