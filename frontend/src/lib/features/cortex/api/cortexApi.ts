import { api, type CortexBootstrapOptions, type CortexBootstrapPayload } from '$lib/api/client';
import { pickTypedApiMethods } from '$lib/api/featureApi';
import type { Connection, Idea } from '$lib/types/cortex';
import type { TeamMember } from '$lib/utils/attractors';

export type { CortexBootstrapOptions, CortexBootstrapPayload };
export type CortexIdea = Idea;
export type CortexConnection = Connection;
export type CreateIdeaInput = Parameters<typeof api.createIdea>[0];
export type UpdateIdeaInput = Parameters<typeof api.updateIdea>[1];
export type CreateConnectionInput = Parameters<typeof api.createConnection>[0];
export type NotifyCortexInput = Parameters<typeof api.notifyCortex>[0];
export type UpdateProfileInput = Parameters<typeof api.updateProfile>[0];
export type CreateProjectContextProfileInput = Parameters<typeof api.createProjectContextProfile>[0];
export type AttachIdeaProjectContextInput = Parameters<typeof api.attachIdeaProjectContext>[1];
export type ConnectProjectContextGitHubInput = Parameters<typeof api.connectProjectContextGitHub>[0];
export type SearchProjectContextGitHubInput = Parameters<typeof api.searchProjectContextGitHub>[0];
export type BindProjectContextGitHubTokenInput = Parameters<typeof api.bindProjectContextGitHubToken>[0];
export type ProjectContextProfile = Awaited<ReturnType<typeof api.listProjectContextProfiles>>[number];
export type ProjectContextAttachment = Awaited<ReturnType<typeof api.listIdeaProjectContext>>[number];
export type UploadedCortexFile = Awaited<ReturnType<typeof api.uploadFile>>;
export type SlashCommand = Awaited<ReturnType<typeof api.slashCommands>>[number];

type CortexApiMethods = {
  cortexBootstrap: (options?: CortexBootstrapOptions) => Promise<CortexBootstrapPayload>;
  listIdeas: (status?: string) => Promise<Idea[]>;
  listArchivedIdeas: (limit?: number) => Promise<Idea[]>;
  getIdea: (id: string) => Promise<Idea>;
  createIdea: (data: CreateIdeaInput) => Promise<Idea>;
  updateIdea: (id: string, data: UpdateIdeaInput) => Promise<Idea>;
  updateIdeaStatus: (id: string, status: string) => Promise<Idea>;
  deleteIdea: typeof api.deleteIdea;
  restoreIdea: (id: string) => Promise<Idea>;
  updatePosition: typeof api.updatePosition;
  batchPositions: typeof api.batchPositions;
  listConnections: () => Promise<Connection[]>;
  ideaConnections: (ideaId: string) => Promise<Connection[]>;
  listThreads: typeof api.listThreads;
  createThread: typeof api.createThread;
  createConnection: typeof api.createConnection;
  deleteConnection: typeof api.deleteConnection;
  listTeamMembers: () => Promise<TeamMember[]>;
  markRead: typeof api.markRead;
  markMentionsSeen: typeof api.markMentionsSeen;
  unreadMentions: typeof api.unreadMentions;
  postPresence: typeof api.postPresence;
  cortexAnalytics: typeof api.cortexAnalytics;
  activityTimeline: typeof api.activityTimeline;
  suggestedIdeas: typeof api.suggestedIdeas;
  slashCommands: () => Promise<SlashCommand[]>;
  delegationStats: typeof api.delegationStats;
  detectBranches: typeof api.detectBranches;
  splitIdea: typeof api.splitIdea;
  uploadFile: (file: File) => Promise<UploadedCortexFile>;
  notifyCortex: typeof api.notifyCortex;
  updateProfile: typeof api.updateProfile;
  listProjectContextProfiles: typeof api.listProjectContextProfiles;
  createProjectContextProfile: typeof api.createProjectContextProfile;
  uploadProjectContextFiles: typeof api.uploadProjectContextFiles;
  listIdeaProjectContext: typeof api.listIdeaProjectContext;
  attachIdeaProjectContext: typeof api.attachIdeaProjectContext;
  connectProjectContextGitHub: typeof api.connectProjectContextGitHub;
  searchProjectContextGitHub: typeof api.searchProjectContextGitHub;
  bindProjectContextGitHubToken: typeof api.bindProjectContextGitHubToken;
};

export const cortexApi = pickTypedApiMethods<CortexApiMethods>([
  'cortexBootstrap',
  'listIdeas',
  'listArchivedIdeas',
  'getIdea',
  'createIdea',
  'updateIdea',
  'updateIdeaStatus',
  'deleteIdea',
  'restoreIdea',
  'updatePosition',
  'batchPositions',
  'listConnections',
  'ideaConnections',
  'listThreads',
  'createThread',
  'createConnection',
  'deleteConnection',
  'listTeamMembers',
  'markRead',
  'markMentionsSeen',
  'unreadMentions',
  'postPresence',
  'cortexAnalytics',
  'activityTimeline',
  'suggestedIdeas',
  'slashCommands',
  'delegationStats',
  'detectBranches',
  'splitIdea',
  'uploadFile',
  'notifyCortex',
  'updateProfile',
  'listProjectContextProfiles',
  'createProjectContextProfile',
  'uploadProjectContextFiles',
  'listIdeaProjectContext',
  'attachIdeaProjectContext',
  'connectProjectContextGitHub',
  'searchProjectContextGitHub',
  'bindProjectContextGitHubToken',
]);

export const {
  cortexBootstrap,
  listIdeas,
  listArchivedIdeas,
  getIdea,
  createIdea,
  updateIdea,
  updateIdeaStatus,
  deleteIdea,
  restoreIdea,
  updatePosition,
  batchPositions,
  listConnections,
  ideaConnections,
  listThreads,
  createThread,
  createConnection,
  deleteConnection,
  listTeamMembers,
  markRead,
  markMentionsSeen,
  unreadMentions,
  postPresence,
  cortexAnalytics,
  activityTimeline,
  suggestedIdeas,
  slashCommands,
  delegationStats,
  detectBranches,
  splitIdea,
  uploadFile,
  notifyCortex,
  updateProfile,
  listProjectContextProfiles,
  createProjectContextProfile,
  uploadProjectContextFiles,
  listIdeaProjectContext,
  attachIdeaProjectContext,
  connectProjectContextGitHub,
  searchProjectContextGitHub,
  bindProjectContextGitHubToken,
} = cortexApi;
