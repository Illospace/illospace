<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import { uploadProjectContextFiles } from '$lib/features/cortex/api/cortexApi';
  import type { ProjectContextResource } from '$lib/utils/projectContext';
  import {
    enableFolderPicker,
    entriesFromDataTransfer,
    entriesFromFileList,
    filterProjectContextUploadEntries,
    uploadedFileResource,
    uploadedFolderResources,
    uploadSkippedSummary,
    type DroppedEntryFile,
  } from '$lib/utils/projectContextLocal';
  import { projectContextErrorDetail } from './projectContextProfiles';

  let {
    mode = 'local',
    onAddResources,
  }: {
    mode?: 'folder' | 'file' | 'local';
    onAddResources?: (resources: ProjectContextResource[]) => void;
  } = $props();

  let localDropActive = $state(false);
  let localDropError = $state('');
  let localUploading = $state(false);
  let localUploadLabel = $state('');
  let localPickerMenuOpen = $state(false);
  let folderInputEl: HTMLInputElement | undefined = $state();
  let fileInputEl: HTMLInputElement | undefined = $state();

  async function uploadLocalProjectFiles(
    entries: DroppedEntryFile[],
    source: string,
    preferFolderResource: boolean,
  ) {
    if (!entries.length || localUploading) return 0;
    const filtered = filterProjectContextUploadEntries(entries);
    const clientSkipped = uploadSkippedSummary(filtered.skippedFiles);
    if (!filtered.entries.length) {
      localDropError = clientSkipped || 'No files found that fit the upload limits.';
      return 0;
    }
    localUploading = true;
    localUploadLabel = filtered.entries.length === 1
      ? 'Uploading 1 file...'
      : `Uploading ${filtered.entries.length} files...`;
    localDropError = '';
    try {
      const result = await uploadProjectContextFiles(
        filtered.entries.map((entry) => entry.file),
        filtered.entries.map((entry) => entry.relativePath),
      );
      const uploadedFiles = Array.isArray(result?.files) ? result.files : [];
      if (!uploadedFiles.length) {
        localDropError = 'No Project Context files were uploaded.';
        return 0;
      }
      const shouldCreateFolders = preferFolderResource || uploadedFiles.some((file: any) => String(file.relative_path ?? '').includes('/'));
      const resources = shouldCreateFolders
        ? uploadedFolderResources(uploadedFiles, source)
        : uploadedFiles.map((file: any) => uploadedFileResource(file, source));
      onAddResources?.(resources);
      const skipped = uploadSkippedSummary([
        ...filtered.skippedFiles,
        ...(Array.isArray(result?.skipped_files) ? result.skipped_files : []),
      ]);
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
      localUploadLabel = '';
    }
  }

  async function handleFileSelect(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    if (files.length) {
      await uploadLocalProjectFiles(
        entriesFromFileList(files),
        'browser-file-upload',
        false,
      );
    }
    input.value = '';
  }

  async function handleFolderSelect(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    if (files.length) {
      await uploadLocalProjectFiles(
        entriesFromFileList(files),
        'browser-folder-upload',
        true,
      );
    }
    input.value = '';
  }

  function toggleLocalPickerMenu() {
    if (localUploading) return;
    localPickerMenuOpen = !localPickerMenuOpen;
    localDropError = '';
  }

  function chooseFiles() {
    localPickerMenuOpen = false;
    fileInputEl?.click();
  }

  function chooseFolder() {
    localPickerMenuOpen = false;
    folderInputEl?.click();
  }

  async function addDroppedFileList(files: File[]) {
    if (!files.length) return 0;
    const entries = entriesFromFileList(files);
    const hasFolderPaths = entries.some((entry) => entry.relativePath.includes('/'));
    return uploadLocalProjectFiles(entries, hasFolderPaths ? 'browser-folder-drop' : 'browser-file-drop', mode === 'folder' || hasFolderPaths);
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
    localPickerMenuOpen = false;
    localDropError = '';
    const transfer = event.dataTransfer;
    if (!transfer) return;
    try {
      localUploadLabel = 'Preparing files...';
      const entries = await entriesFromDataTransfer(transfer);
      const hasFolderPaths = entries.some((entry) => entry.relativePath.includes('/'));
      const added = entries.length
        ? await uploadLocalProjectFiles(
          entries,
          hasFolderPaths ? 'browser-folder-drop' : 'browser-file-drop',
          mode === 'folder' || hasFolderPaths,
        )
        : await addDroppedFileList(Array.from(transfer.files ?? []));
      if (!added && !localDropError) {
        localDropError = 'No files found in this drop.';
      }
    } catch {
      localDropError = 'Could not read dropped files.';
    } finally {
      if (!localUploading) localUploadLabel = '';
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
    aria-label="Drop files or folders"
    aria-expanded={localPickerMenuOpen}
    aria-haspopup="menu"
    onclick={toggleLocalPickerMenu}
    ondragenter={handleLocalDragEnter}
    ondragover={handleLocalDragOver}
    ondragleave={handleLocalDragLeave}
    ondrop={(event) => void handleLocalDrop(event)}
  >
    <ConstellationIcon name="paperclip" size={16} stroke={2} />
    <span>
      <strong>
        {localUploading
          ? (localUploadLabel || 'Uploading files...')
          : (localDropActive ? 'Release to upload' : 'Drop files or folders')}
      </strong>
      <small>Select files, select a folder, or drop either here</small>
    </span>
  </button>
  {#if localPickerMenuOpen}
    <div class="local-picker-menu" role="menu" aria-label="Choose local resource type">
      <button type="button" role="menuitem" disabled={localUploading} onclick={chooseFiles}>
        <ConstellationIcon name="file" size={16} stroke={2} />
        <span><strong>Files</strong><small>Select one or more files</small></span>
      </button>
      <button type="button" role="menuitem" disabled={localUploading} onclick={chooseFolder}>
        <ConstellationIcon name="folder" size={16} stroke={2} />
        <span><strong>Folder</strong><small>Preserve nested paths</small></span>
      </button>
    </div>
  {/if}
  {#if localDropError}
    <p class="project-context-error">{localDropError}</p>
  {/if}
</div>
