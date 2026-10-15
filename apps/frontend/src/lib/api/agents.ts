// Public agent API. Keep this import path stable for components and callers.
// Domain modules own their wire types and operations; internal modules import
// those directly rather than depending on this facade.

export type { AgentConfigApi, AgentMcpServerApi } from './agents/config';

export { getAgentConfig } from './agents/config';

export type {
  CreateGitHubRepositoryRequest,
  CreatedGitHubRepository,
  AgentIntegrationProvider,
  SourceControlLoadStatus,
  AgentIntegrationAccountApi,
  AgentIntegrationRepositoryApi,
  AgentIntegrationProviderApi,
  AgentIntegrationsApi,
  GitHubConnectionApi,
  RepoBranchesApi,
} from './agents/integrations';

export {
  createGitHubRepository,
  getAgentIntegrations,
  connectGitHub,
  connectGitLab,
  disconnectGitHubAccount,
  disconnectAgentIntegration,
  listRepoBranches,
} from './agents/integrations';

export type {
  AgentJobApiState,
  AgentJobApi,
  McpBindingInputApi,
  AgentPluginInputApi,
  AgentThreadPublicationApi,
  CreateAgentJobRequest,
} from './agents/jobs';

export {
  getAgentJob,
  createAgentJob,
  cancelAgentJob,
  forkAgentJob,
  publishAgentThread,
} from './agents/jobs';

export type {
  AgentThreadMessageApi,
  AgentThreadApi,
  AgentThreadStatsApi,
  AgentConversationApi,
  AgentProjectApi,
} from './agents/threads';

export {
  listAgentConversations,
  listAgentProjects,
  getAgentJobThread,
  getAgentThreadStats,
} from './agents/threads';

export type {
  AgentThreadArchiveApi,
  AgentSessionState,
  AgentSessionApi,
  AgentSessionProjectApi,
  AgentSessionListApi,
  AgentSessionListOptions,
  AgentThreadPinApi,
} from './agents/sessions';

export {
  listAgentSessions,
  AGENT_SESSION_SELECTION_LIMIT,
  setAgentSessionsArchived,
  deleteAgentSessions,
  archiveAgentJob,
  restoreAgentJob,
  renameAgentJob,
  pinAgentJob,
  unpinAgentJob,
} from './agents/sessions';

export type {
  GoalApi,
  GoalPauseReason,
  GoalReportParse,
  GoalReportOutcome,
  GoalInputApi,
  GoalLimitsApi,
  GoalWriteApi,
  GoalResponseApi,
  GoalClearedApi,
} from './agents/goals';

export {
  getAgentGoal,
  setAgentGoal,
  pauseAgentGoal,
  resumeAgentGoal,
  clearAgentGoal,
} from './agents/goals';

export type {
  ToolCallResourcesApi,
  ProcessObservationApi,
  AttemptResourcesApi,
  ResourceMetricStatus,
  TurnResourceReasonCode,
  TurnResourceState,
  TurnResourceWindowApi,
  TurnCpuResourcesApi,
  TurnMemoryResourcesApi,
  TurnStorageIoResourcesApi,
  TurnResourceSummaryApi,
  ResourceMetricBriefApi,
  RunResourceSummaryApi,
  AgentAttemptSummaryApi,
  AgentAttemptTelemetryApi,
} from './agents/resources';

export { getAgentAttemptTelemetry } from './agents/resources';

export type {
  AgentLedgerStatus,
  AgentLedgerTotalsApi,
  AgentLedgerUnknownRowsApi,
  AgentAttemptUsageApi,
  AgentJobUsageApi,
  AgentMessageRequestApi,
} from './agents/usage';

export { getAgentMessageRequest, getAgentJobUsage, refreshAgentJobUsage } from './agents/usage';

export type {
  AgentJobFileStatus,
  AgentJobFileEntryApi,
  AgentJobDirectoryApi,
  AgentJobFileApi,
  AgentJobSymlinkApi,
  AgentJobFilesApi,
  AgentGitWorkspaceApi,
} from './agents/workspace';

export {
  getAgentJobFiles,
  writeAgentJobFile,
  getAgentJobGit,
  downloadAgentWorkspace,
} from './agents/workspace';

export type {
  AgentTerminalApi,
  AgentTerminalListApi,
  AgentTerminalOutputEvent,
  AgentTerminalExitEvent,
  AgentTerminalResetEvent,
  AgentTerminalAttachedEvent,
  StreamAgentTerminalOptions,
} from './agents/terminals';

export {
  listAgentTerminals,
  createAgentTerminal,
  writeAgentTerminalInput,
  resizeAgentTerminal,
  deleteAgentTerminal,
  streamAgentTerminal,
} from './agents/terminals';

export type {
  AgentJobEventApi,
  AgentThreadEventApi,
  StreamAgentJobOptions,
  ParsedFrame,
} from './agents/events';

export {
  listAgentThreadEvents,
  listAllAgentThreadEvents,
  parseFrame,
  streamAgentJob,
} from './agents/events';
