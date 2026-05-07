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
  return new Promise((resolve, reject) => {
    entry.file(
      (file) => resolve({ file, relativePath }),
      (error) => reject(error),
    );
  });
}

export function readDirectoryEntries(reader: FileSystemDirectoryReaderLike): Promise<FileSystemEntryLike[]> {
  return new Promise((resolve, reject) => {
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
  });
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
