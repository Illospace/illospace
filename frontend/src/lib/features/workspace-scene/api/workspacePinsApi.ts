import { pickApiMethods } from '$lib/api/featureApi';

export type {
  WorkspacePinCreateInput,
  WorkspacePinRead,
  WorkspacePinUpdateInput,
} from '$lib/api/client';

export const workspacePinsApi = pickApiMethods([
  'listWorkspacePins',
  'createWorkspacePin',
  'updateWorkspacePin',
  'deleteWorkspacePin',
  'archiveWorkspacePin',
] as const);

export const {
  listWorkspacePins,
  createWorkspacePin,
  updateWorkspacePin,
  deleteWorkspacePin,
  archiveWorkspacePin,
} = workspacePinsApi;
