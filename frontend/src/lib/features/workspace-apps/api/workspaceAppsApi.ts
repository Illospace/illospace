import { api, type WorkspaceAppStateRead } from '$lib/api/client';
import { pickApiMethods } from '$lib/api/featureApi';

export type {
  DomainCreateInput,
  DomainFieldCreateInput,
  DomainFieldRead,
  DomainObjectCreateInput,
  DomainObjectRead,
  DomainEventRead,
  DomainRecordRead,
  DomainRelationRead,
  DomainRelationTypeRead,
  DomainSchemaRead,
  DomainSummaryRead,
  WorkspaceAppCreateInput,
  WorkspaceAppActionRunInput,
  WorkspaceAppActionRunRead,
  WorkspaceAppBindingRunInput,
  WorkspaceAppBindingRunRead,
  WorkspaceAppCollaborationRead,
  WorkspaceAppRead,
  WorkspaceAppEventCreateInput,
  WorkspaceAppEventRead,
  WorkspaceAppEventsRead,
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
export type ListDomainRelationsOptions = Parameters<typeof api.listDomainRelations>[1];
export type ListDomainEventsOptions = Parameters<typeof api.listDomainEvents>[1];

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
  'getWorkspaceAppCollaboration',
  'listWorkspaceAppEvents',
  'appendWorkspaceAppEvent',
  'runWorkspaceAppAction',
  'runWorkspaceAppBinding',
  'listDomains',
  'createDomain',
  'getDomain',
  'removeDomain',
  'listDomainRecords',
  'getDomainRecord',
  'createDomainRecord',
  'updateDomainRecord',
  'removeDomainRecord',
  'listDomainRelations',
  'createDomainRelation',
  'removeDomainRelation',
  'listDomainEvents',
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
  getWorkspaceAppCollaboration,
  listWorkspaceAppEvents,
  appendWorkspaceAppEvent,
  runWorkspaceAppAction,
  runWorkspaceAppBinding,
  listDomains,
  createDomain,
  getDomain,
  removeDomain,
  listDomainRecords,
  getDomainRecord,
  createDomainRecord,
  updateDomainRecord,
  removeDomainRecord,
  listDomainRelations,
  createDomainRelation,
  removeDomainRelation,
  listDomainEvents,
} = workspaceAppsApi;
