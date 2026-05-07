<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import { uploadProjectContextFiles } from '$lib/features/cortex/api/cortexApi';
  import type { ProjectContextResource } from '$lib/utils/projectContext';
  import {
    enableFolderPicker,
    readDroppedEntry,
    relativePathForFile,
    uploadedFileResource,
    uploadedFolderResources,
    uploadSkippedSummary,
    type DataTransferItemWithEntry,
    type DroppedEntryFile,
    type FileSystemEntryLike,
  } from '$lib/utils/projectContextLocal';
  import { projectContextErrorDetail } from './projectContextProfiles';

  let {
    mode = 'folder',
    onAddResources,
  }: {
    mode?: 'folder' | 'file';
    onAddResources?: (resources: ProjectContextResource[]) => void;
  } = $props();

  let localDropActive = $state(false);
  let localDropError = $state('');
  let localUploading = $state(false);
  let folderInputEl: HTMLInputElement | undefined = $state();
  let fileInputEl: HTMLInputElement | undefined = $state();

  async function uploadLocalProjectFiles(
    entries: DroppedEntryFile[],
    source: string,
    preferFolderResource: boolean,
  ) {
    if (!entries.length || localUploading) return 0;
    localUploading = true;
    localDropError = '';
    try {
      const result = await uploadProjectContextFiles(
        entries.map((entry) => entry.file),
        entries.map((entry) => entry.relativePath),
      );
      const uploadedFiles = Array.isArray(result?.files) ? result.files : [];
      if (!uploadedFiles.length) {
        localDropError = 'No readable Project Context files were uploaded.';
        return 0;
      }
      const shouldCreateFolders = preferFolderResource || uploadedFiles.some((file: any) => String(file.relative_path ?? '').includes('/'));
      const resources = shouldCreateFolders
        ? uploadedFolderResources(uploadedFiles, source)
        : uploadedFiles.map((file: any) => uploadedFileResource(file, source));
      onAddResources?.(resources);
      const skipped = uploadSkippedSummary(result?.skipped_files);
      if (skipped) localDropError = skipped;
      return resources.length;
    } catch (err: any) {
      const detail = err?.detail;
      if (typeof detail === 'object' && Array.isArray(detail?.skipped_files)) {
        localDropError = uploadSkippedSummary(detail.skipped_files) || String(detail?.error ?? 'Could not upload these files.');
      } else {
        localDropError = projectContextErrorDetail(err, 'Could not upload these files.');
      }
      return 0;
    } finally {
      localUploading = false;
    }
  }

  async function handleFolderSelect(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    if (files.length) {
      await uploadLocalProjectFiles(
        files.map((file) => ({ file, relativePath: relativePathForFile(file) })),
        'browser-folder-upload',
        true,
      );
    }
    input.value = '';
  }

  async function handleFileSelect(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    if (files.length) {
      await uploadLocalProjectFiles(
        files.map((file) => ({ file, relativePath: relativePathForFile(file) })),
        'browser-file-upload',
        false,
      );
    }
    input.value = '';
  }

  async function addDroppedFileList(files: File[]) {
    if (!files.length) return 0;
    const entries = files.map((file) => ({ file, relativePath: relativePathForFile(file) }));
    const hasFolderPaths = entries.some((entry) => entry.relativePath.includes('/'));
    return uploadLocalProjectFiles(entries, hasFolderPaths ? 'browser-folder-drop' : 'browser-file-drop', mode === 'folder' || hasFolderPaths);
  }

  async function addDroppedEntries(entries: FileSystemEntryLike[]) {
    let added = 0;
    for (const entry of entries) {
      const files = await readDroppedEntry(entry);
      if (!files.length) continue;
      added += await uploadLocalProjectFiles(
        files,
        mode === 'file' && entry.isFile ? 'browser-file-drop' : 'browser-folder-drop',
        !(mode === 'file' && entry.isFile),
      );
    }
    return added;
  }

  function prepareLocalDrop(event: DragEvent) {
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy';
    }
  }

  function handleLocalDragEnter(event: DragEvent) {
    prepareLocalDrop(event);
    localDropActive = true;
    localDropError = '';
  }

  function handleLocalDragOver(event: DragEvent) {
    prepareLocalDrop(event);
    localDropActive = true;
  }

  function handleLocalDragLeave(event: DragEvent) {
    prepareLocalDrop(event);
    const nextTarget = event.relatedTarget;
    if (!(nextTarget instanceof Node) || !(event.currentTarget as HTMLElement).contains(nextTarget)) {
      localDropActive = false;
    }
  }

  async function handleLocalDrop(event: DragEvent) {
    prepareLocalDrop(event);
    localDropActive = false;
    localDropError = '';
    const transfer = event.dataTransfer;
    if (!transfer) return;
    try {
      const entries = Array.from(transfer.items ?? []).reduce<FileSystemEntryLike[]>((acc, item) => {
        const entry = (item as unknown as DataTransferItemWithEntry).webkitGetAsEntry?.();
        if (entry) acc.push(entry);
        return acc;
      }, []);
      const added = entries.length
        ? await addDroppedEntries(entries)
        : await addDroppedFileList(Array.from(transfer.files ?? []));
      if (!added) {
        localDropError = 'No files found in this drop.';
      }
    } catch {
      localDropError = 'Could not read dropped files.';
    }
  }
</script>

<div class="connector-panel local-resource-panel">
  <input
    class="native-picker-input"
    type="file"
    multiple
    bind:this={folderInputEl}
    use:enableFolderPicker
    onchange={handleFolderSelect}
    aria-hidden="true"
    tabindex="-1"
  />
  <input
    class="native-picker-input"
    type="file"
    multiple
    bind:this={fileInputEl}
    onchange={handleFileSelect}
    aria-hidden="true"
    tabindex="-1"
  />
  <button
    type="button"
    class="local-drop-zone"
    class:active={localDropActive}
    disabled={localUploading}
    aria-label={mode === 'folder' ? 'Upload a folder tree' : 'Upload individual files'}
    onclick={() => mode === 'folder' ? folderInputEl?.click() : fileInputEl?.click()}
    ondragenter={handleLocalDragEnter}
    ondragover={handleLocalDragOver}
    ondragleave={handleLocalDragLeave}
    ondrop={(event) => void handleLocalDrop(event)}
  >
    <ConstellationIcon name="paperclip" size={16} stroke={2} />
    <span>
      <strong>
        {localUploading
          ? 'Uploading snapshot...'
          : (localDropActive ? 'Release to upload' : (mode === 'folder' ? 'Choose or drop a folder tree' : 'Choose or drop files'))}
      </strong>
      <small>{mode === 'folder' ? 'Best when related files live under one root folder' : 'Best for standalone docs, screenshots, or small source files'}</small>
    </span>
  </button>
  {#if localDropError}
    <p class="project-context-error">{localDropError}</p>
  {/if}
</div>
