import type { CortexUserMenuAnchor } from '$lib/features/cortex/components/menus/UserMenu.svelte';
import type { CortexWorkspaceMenuAnchor } from '$lib/features/cortex/components/menus/WorkspaceMenu.svelte';
import type { CortexWorkspacePinMenuAnchor } from '$lib/features/cortex/components/menus/WorkspacePinMenu.svelte';
import type { CortexWorkspacePoint } from '$lib/features/workspace-scene/domain/workspacePoint';
import type { WorkspacePinRead } from '$lib/features/workspace-scene/api/workspacePinsApi';

export class WorkspaceOverlayController {
  composerContext = $state<CortexWorkspacePoint | null>(null);
  activeWorkspaceAppId = $state<string | null>(null);
  userMenuAnchor = $state<CortexUserMenuAnchor | null>(null);
  workspaceMenuAnchor = $state<CortexWorkspaceMenuAnchor | null>(null);
  pinMenuAnchor = $state<CortexWorkspacePinMenuAnchor | null>(null);
  pinMenuSaving = $state(false);
  pinMenuDeleting = $state(false);
  archiveDragActive = $state(false);
  archiveDropActive = $state(false);

  setComposerContext(point: CortexWorkspacePoint | null) {
    this.composerContext = point;
  }

  openWorkspaceApp(appId: string) {
    this.activeWorkspaceAppId = appId;
  }

  closeWorkspaceApp() {
    this.activeWorkspaceAppId = null;
  }

  openWorkspaceAppOverlay(appId: string) {
    this.activeWorkspaceAppId = appId;
    this.closeWorkspaceAndPinMenus();
  }

  openWorkspaceMenu(anchor: CortexWorkspaceMenuAnchor) {
    this.workspaceMenuAnchor = anchor;
    this.pinMenuAnchor = null;
    this.userMenuAnchor = null;
  }

  closeWorkspaceMenu() {
    this.workspaceMenuAnchor = null;
  }

  openUserMenu(anchor: CortexUserMenuAnchor) {
    this.userMenuAnchor = anchor;
    this.workspaceMenuAnchor = null;
    this.pinMenuAnchor = null;
  }

  closeUserMenu() {
    this.userMenuAnchor = null;
  }

  openPinMenu(anchor: CortexWorkspacePinMenuAnchor) {
    this.pinMenuAnchor = anchor;
    this.workspaceMenuAnchor = null;
    this.userMenuAnchor = null;
  }

  closePinMenu() {
    this.pinMenuAnchor = null;
    this.pinMenuSaving = false;
    this.pinMenuDeleting = false;
  }

  closeWorkspaceAndPinMenus() {
    this.workspaceMenuAnchor = null;
    this.pinMenuAnchor = null;
  }

  setPinMenuSaving(saving: boolean) {
    this.pinMenuSaving = saving;
  }

  setPinMenuDeleting(deleting: boolean) {
    this.pinMenuDeleting = deleting;
  }

  updatePinMenuPin(pinId: string, pin: WorkspacePinRead) {
    if (this.pinMenuAnchor?.pin.id === pinId) {
      this.pinMenuAnchor = { ...this.pinMenuAnchor, pin };
    }
  }

  setArchiveDragState(state: { active: boolean; over: boolean }) {
    this.archiveDragActive = state.active;
    this.archiveDropActive = state.over;
  }

  resetArchiveDragState() {
    this.archiveDragActive = false;
    this.archiveDropActive = false;
  }

}

export function createWorkspaceOverlayController(): WorkspaceOverlayController {
  return new WorkspaceOverlayController();
}
