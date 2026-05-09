import { api } from '$lib/api/client';
import { pickTypedApiMethods } from '$lib/api/featureApi';
import type { Connection, Idea, StreamItem } from '$lib/types/cortex';

export type ThreadMessageInput = Parameters<typeof api.addThreadMessage>[1];
export type ThreadMessage = Awaited<ReturnType<typeof api.addThreadMessage>>;
export type ThreadRunHistoryItem = Awaited<ReturnType<typeof api.runHistory>>[number];
export type RunDecisionResponse = Awaited<ReturnType<typeof api.approveRun>>;
export type RunTool = Awaited<ReturnType<typeof api.runTools>>[number];
export type RunGraph = Awaited<ReturnType<typeof api.runGraph>>;
export type RunStatus = Awaited<ReturnType<typeof api.runStatus>>;
export type ActiveOpsRun = Awaited<ReturnType<typeof api.opsActive>>[number];
export type RecentOpsRun = Awaited<ReturnType<typeof api.opsRecent>>[number];
export type SkillFeedbackInput = Parameters<typeof api.skillFeedback>[1];
export type UploadedThreadFile = Awaited<ReturnType<typeof api.uploadFile>>;
export type ThreadProjectContextAttachment = Awaited<ReturnType<typeof api.listIdeaProjectContext>>[number];
export type AttachThreadProjectContextInput = Parameters<typeof api.attachIdeaProjectContext>[1];
export type ThreadActivityTimelineItem = Awaited<ReturnType<typeof api.activityTimeline>>[number];
export type IdeaAudit = Awaited<ReturnType<typeof api.ideaAudit>>;
export type IdeaAuditAnalysisResult = Awaited<ReturnType<typeof api.ideaAuditAnalysisResult>>;
export type GeneratedThreadTitle = Awaited<ReturnType<typeof api.generateTitle>>;

type ThreadApiMethods = {
  unifiedStream: (ideaId: string, includeDebug?: boolean) => Promise<StreamItem[]>;
  addThreadMessage: (ideaId: string, data: ThreadMessageInput) => Promise<ThreadMessage>;
  runHistory: (ideaId: string, includeDebug?: boolean) => Promise<ThreadRunHistoryItem[]>;
  approveRun: typeof api.approveRun;
  denyRun: typeof api.denyRun;
  cancelRun: typeof api.cancelRun;
  steerRun: typeof api.steerRun;
  cancelAllRuns: typeof api.cancelAllRuns;
  runStatus: () => Promise<RunStatus>;
  runGraph: (id: number) => Promise<RunGraph>;
  runTools: (id: number) => Promise<RunTool[]>;
  skillFeedback: typeof api.skillFeedback;
  opsActive: () => Promise<ActiveOpsRun[]>;
  opsRecent: (limit?: number, includeDebug?: boolean) => Promise<RecentOpsRun[]>;
  getIdea: (id: string) => Promise<Idea>;
  ideaConnections: (ideaId: string) => Promise<Connection[]>;
  activityTimeline: typeof api.activityTimeline;
  generateTitle: typeof api.generateTitle;
  uploadFile: (file: File) => Promise<UploadedThreadFile>;
  listIdeaProjectContext: typeof api.listIdeaProjectContext;
  attachIdeaProjectContext: typeof api.attachIdeaProjectContext;
  ideaAudit: typeof api.ideaAudit;
  ideaAuditAnalyze: typeof api.ideaAuditAnalyze;
  ideaAuditAnalysisResult: typeof api.ideaAuditAnalysisResult;
  auditApply: typeof api.auditApply;
  auditEval: typeof api.auditEval;
};

export const threadApi = pickTypedApiMethods<ThreadApiMethods>([
  'unifiedStream',
  'addThreadMessage',
  'runHistory',
  'approveRun',
  'denyRun',
  'cancelRun',
  'steerRun',
  'cancelAllRuns',
  'runStatus',
  'runGraph',
  'runTools',
  'skillFeedback',
  'opsActive',
  'opsRecent',
  'getIdea',
  'ideaConnections',
  'activityTimeline',
  'uploadFile',
  'listIdeaProjectContext',
  'attachIdeaProjectContext',
  'ideaAudit',
  'ideaAuditAnalyze',
  'ideaAuditAnalysisResult',
  'auditApply',
  'auditEval',
]);

export const {
  unifiedStream,
  addThreadMessage,
  runHistory,
  approveRun,
  denyRun,
  cancelRun,
  steerRun,
  cancelAllRuns,
  runStatus,
  runGraph,
  runTools,
  skillFeedback,
  opsActive,
  opsRecent,
  getIdea,
  ideaConnections,
  activityTimeline,
  generateTitle,
  uploadFile,
  listIdeaProjectContext,
  attachIdeaProjectContext,
  ideaAudit,
  ideaAuditAnalyze,
  ideaAuditAnalysisResult,
  auditApply,
  auditEval,
} = threadApi;
