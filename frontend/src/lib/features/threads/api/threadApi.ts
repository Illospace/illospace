import { api } from '$lib/api/client';
import { pickTypedApiMethods } from '$lib/api/featureApi';
import type { Connection, Idea, StreamItem } from '$lib/types/cortex';

export type ThreadMessageInput = Parameters<typeof api.addThreadMessage>[1];
export type ThreadMessage = Awaited<ReturnType<typeof api.addThreadMessage>>;
export type ThreadRunHistoryItem = Awaited<ReturnType<typeof api.runHistory>>[number];
export type RunDecisionResponse = Awaited<ReturnType<typeof api.approveRun>>;
export type RunTool = Awaited<ReturnType<typeof api.runTools>>[number];
export type ThreadTraceZipDownload = Awaited<ReturnType<typeof api.downloadThreadTraceZip>>;
export type ThreadHandoffSummary = Awaited<ReturnType<typeof api.threadHandoffSummary>>;
export type RunGraph = Awaited<ReturnType<typeof api.runGraph>>;
export type RunTraceZipDownload = Awaited<ReturnType<typeof api.downloadRunTraceZip>>;
export type RunStatus = Awaited<ReturnType<typeof api.runStatus>>;
export type SkillFeedbackInput = Parameters<typeof api.skillFeedback>[1];
export type UploadedThreadFile = Awaited<ReturnType<typeof api.uploadFile>>;
export type UploadPreview = Awaited<ReturnType<typeof api.previewUpload>>;
export type ThreadProjectContextAttachment = Awaited<ReturnType<typeof api.listIdeaProjectContext>>[number];
export type AttachThreadProjectContextInput = Parameters<typeof api.attachIdeaProjectContext>[1];
export type ThreadProjectDraftState = Awaited<ReturnType<typeof api.getIdeaProjectDraftState>>;
export type ThreadActivityTimelineItem = Awaited<ReturnType<typeof api.activityTimeline>>[number];
export type IdeaAudit = Awaited<ReturnType<typeof api.ideaAudit>>;
export type IdeaAuditAnalysisResult = Awaited<ReturnType<typeof api.ideaAuditAnalysisResult>>;
export type GeneratedThreadTitle = Awaited<ReturnType<typeof api.generateTitle>>;
export type ThreadDiscussionComment = Awaited<ReturnType<typeof api.listThreadDiscussion>>[number];
export type ThreadDiscussionCreateInput = Parameters<typeof api.postThreadDiscussionComment>[1];
export type ThreadDiscussionCreateResult = Awaited<ReturnType<typeof api.postThreadDiscussionComment>>;

type ThreadApiMethods = {
  unifiedStream: (ideaId: string, includeDebug?: boolean) => Promise<StreamItem[]>;
  addThreadMessage: (ideaId: string, data: ThreadMessageInput) => Promise<ThreadMessage>;
  runHistory: (ideaId: string, includeDebug?: boolean) => Promise<ThreadRunHistoryItem[]>;
  approveRun: typeof api.approveRun;
  denyRun: typeof api.denyRun;
  steerRun: typeof api.steerRun;
  cancelAllRuns: typeof api.cancelAllRuns;
  runStatus: () => Promise<RunStatus>;
  runGraph: (id: number) => Promise<RunGraph>;
  runTools: (id: number) => Promise<RunTool[]>;
  threadHandoffSummary: typeof api.threadHandoffSummary;
  downloadThreadTraceZip: typeof api.downloadThreadTraceZip;
  downloadRunTraceZip: typeof api.downloadRunTraceZip;
  skillFeedback: typeof api.skillFeedback;
  getIdea: (id: string) => Promise<Idea>;
  ideaConnections: (ideaId: string) => Promise<Connection[]>;
  listThreadDiscussion: typeof api.listThreadDiscussion;
  postThreadDiscussionComment: typeof api.postThreadDiscussionComment;
  activityTimeline: typeof api.activityTimeline;
  generateTitle: typeof api.generateTitle;
  uploadFile: (file: File) => Promise<UploadedThreadFile>;
  previewUpload: typeof api.previewUpload;
  listIdeaProjectContext: typeof api.listIdeaProjectContext;
  attachIdeaProjectContext: typeof api.attachIdeaProjectContext;
  getIdeaProjectDraftState: typeof api.getIdeaProjectDraftState;
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
  'steerRun',
  'cancelAllRuns',
  'runStatus',
  'runGraph',
  'runTools',
  'threadHandoffSummary',
  'downloadThreadTraceZip',
  'downloadRunTraceZip',
  'skillFeedback',
  'getIdea',
  'ideaConnections',
  'listThreadDiscussion',
  'postThreadDiscussionComment',
  'activityTimeline',
  'generateTitle',
  'uploadFile',
  'previewUpload',
  'listIdeaProjectContext',
  'attachIdeaProjectContext',
  'getIdeaProjectDraftState',
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
  steerRun,
  cancelAllRuns,
  runStatus,
  runGraph,
  runTools,
  threadHandoffSummary,
  downloadThreadTraceZip,
  downloadRunTraceZip,
  skillFeedback,
  getIdea,
  ideaConnections,
  listThreadDiscussion,
  postThreadDiscussionComment,
  activityTimeline,
  generateTitle,
  uploadFile,
  previewUpload,
  listIdeaProjectContext,
  attachIdeaProjectContext,
  getIdeaProjectDraftState,
  ideaAudit,
  ideaAuditAnalyze,
  ideaAuditAnalysisResult,
  auditApply,
  auditEval,
} = threadApi;
