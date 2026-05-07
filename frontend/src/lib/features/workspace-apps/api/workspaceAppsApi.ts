import { api, type WorkspaceAppStateRead } from '$lib/api/client';
import { pickApiMethods } from '$lib/api/featureApi';

export type {
  DomainCreateInput,
  DomainFieldCreateInput,
  DomainFieldRead,
  DomainObjectCreateInput,
  DomainObjectRead,
  DomainRecordRead,
  DomainRelationTypeRead,
  DomainSchemaRead,
  DomainSummaryRead,
  WorkspaceAppCreateInput,
  WorkspaceAppRead,
  WorkspaceAppStateRead,
  WorkspaceAppUpdateInput,
  WorkspaceAppVersionRead,
} from '$lib/api/client';

export type WorkspaceAppStateData = WorkspaceAppStateRead['data'];
export type DomainRecordCreateInput = Parameters<typeof api.createDomainRecord>[2];
export type DomainRecordUpdateInput = Parameters<typeof api.updateDomainRecord>[2];
export type RemoveDomainMode = Parameters<typeof api.removeDomain>[1];
export type RemoveDomainRecordMode = Parameters<typeof api.removeDomainRecord>[2];
export type ListDomainRecordsOptions = Parameters<typeof api.listDomainRecords>[1];

export const workspaceAppsApi = pickApiMethods([
  'listWorkspaceApps',
  'createWorkspaceApp',
  'getWorkspaceApp',
  'listArchivedWorkspaceApps',
  'updateWorkspaceApp',
  'archiveWorkspaceApp',
  'restoreWorkspaceApp',
  'getWorkspaceAppState',
  'updateWorkspaceAppState',
  'listDomains',
  'createDomain',
  'getDomain',
  'removeDomain',
  'listDomainRecords',
  'getDomainRecord',
  'createDomainRecord',
  'updateDomainRecord',
  'removeDomainRecord',
] as const);

export const {
  listWorkspaceApps,
  createWorkspaceApp,
  getWorkspaceApp,
  listArchivedWorkspaceApps,
  updateWorkspaceApp,
  archiveWorkspaceApp,
  restoreWorkspaceApp,
  getWorkspaceAppState,
  updateWorkspaceAppState,
  listDomains,
  createDomain,
  getDomain,
  removeDomain,
  listDomainRecords,
  getDomainRecord,
  createDomainRecord,
  updateDomainRecord,
  removeDomainRecord,
} = workspaceAppsApi;
