'use client';

import { useT } from '@/components/providers';
import type {
  AgentConfigApi,
  AgentJobApi,
  AgentJobEventApi,
  AgentThreadEventApi,
} from '@/lib/api/agents';
import { memo, useCallback, useRef, type CSSProperties } from 'react';
import { WorkspaceEnvironment } from '../environment/WorkspaceEnvironment';
import { PaneResizer } from '../PaneResizer';
import type { RecordedFileSelection } from '../recordedTurnOutputs';
import type { AgentJob } from '../types';
import type { SideChatTab, WorkspaceTab } from '../useWorkspaceTabs';
import { WorkspaceTabBar } from '../WorkspaceTabBar';
import { GitPanel } from './GitPanel';
import {
  RecordedDiffPanel,
  SideChat,
  TerminalWorkspace,
  TrajectoryInspector,
  WorkspaceFiles,
} from './LazyPanels';
import type { JobDetailPanels } from './useJobDetailPanels';

/** Everything the workspace panels read from the turn. */
const WORKSPACE_JOB_FIELDS = [
  'id',
  'threadId',
  'turnNo',
  'state',
  'archivedAt',
  'repo',
  'branch',
  'baseRef',
  'baseSha',
  'prUrl',
  'diffFiles',
  'diffFileDetails',
  'workspaceGitChanges',
  'workspaceGitRevision',
  'workspaceGitMetadataPending',
  'workspaceGitAvailable',
  'sandboxId',
  'workspaceShared',
  'model',
  'reasoningEffort',
  'runtime',
] as const;

type WorkspaceJob = Pick<AgentJob, (typeof WORKSPACE_JOB_FIELDS)[number]>;

/**
 * The turn as the workspace panels see it: the same object until one of their
 * fields changes. The page receives a new job for every streamed event, and a
 * retained diff, file preview or side chat has nothing new to draw for most.
 */
function useWorkspaceJob(job: AgentJob): WorkspaceJob {
  const previous = useRef<WorkspaceJob | null>(null);
  const current = previous.current;
  if (current && WORKSPACE_JOB_FIELDS.every((field) => Object.is(current[field], job[field]))) {
    return current;
  }
  const next = Object.fromEntries(
    WORKSPACE_JOB_FIELDS.map((field) => [field, job[field]]),
  ) as WorkspaceJob;
  previous.current = next;
  return next;
}

/**
 * The workspace's tabs. They stay mounted while the workspace is closed, which
 * is what keeps terminals, previews, side chat drafts and scroll positions, so
 * they are drawn again only when something they show changes: not for every
 * keystroke and streamed event on the page.
 */
function WorkspacePanelsView({
  job,
  userId,
  config,
  threadJobs,
  workspaceKey,
  open,
  tab,
  chats,
  gitInitialized,
  terminalInitialized,
  filesInitialized,
  requestedPath,
  requestToken,
  recordedFile,
  terminalWorkspaceReady,
  onSelect,
  onAddChat,
  onCloseChat,
  onBack,
  onCloseRecordedFile,
  onReview,
  onWatchPr,
}: {
  job: WorkspaceJob;
  userId?: string;
  config: AgentConfigApi | null;
  threadJobs: AgentJobApi[];
  workspaceKey: string;
  open: boolean;
  tab: WorkspaceTab;
  chats: SideChatTab[];
  gitInitialized: boolean;
  terminalInitialized: boolean;
  filesInitialized: boolean;
  requestedPath?: string;
  requestToken?: number;
  recordedFile: RecordedFileSelection | null;
  terminalWorkspaceReady: boolean;
  onSelect: (tab: WorkspaceTab) => void;
  onAddChat: () => WorkspaceTab;
  onCloseChat: (id: SideChatTab['id']) => WorkspaceTab;
  onBack: () => void;
  onCloseRecordedFile: () => void;
  onReview?: () => void;
  onWatchPr?: () => void;
}) {
  const t = useT();
  const isArchived = Boolean(job.archivedAt);
  return (
    <>
      <WorkspaceTabBar
        key={`${userId}:${job.threadId ?? job.id}`}
        selected={tab}
        chats={chats}
        diffCount={job.diffFiles.length}
        onSelect={onSelect}
        onAdd={onAddChat}
        onClose={onCloseChat}
        onBack={onBack}
      />
      <div
        id="workspace-panel-environment"
        role="tabpanel"
        aria-labelledby="workspace-tab-environment"
        hidden={tab !== 'environment'}
        className="min-h-0 flex-1 overflow-y-auto"
      >
        {tab === 'environment' && (
          <WorkspaceEnvironment
            key={job.threadId ?? job.id}
            jobId={job.id}
            repo={job.repo}
            onTerminal={() => onSelect('terminal')}
            visible={open}
          />
        )}
      </div>
      <div
        id="workspace-panel-git"
        role="tabpanel"
        aria-labelledby="workspace-tab-git"
        hidden={tab !== 'git'}
        className="min-h-0 flex-1 overflow-y-auto p-5"
      >
        {!gitInitialized ? null : recordedFile ? (
          <RecordedDiffPanel
            selection={recordedFile}
            turnNo={
              threadJobs.find((turn) => turn.id === recordedFile.jobId)?.turn_no ?? job.turnNo ?? 1
            }
            onClose={onCloseRecordedFile}
          />
        ) : (
          <GitPanel
            job={job}
            active={open && tab === 'git'}
            onReview={onReview}
            onWatchPr={onWatchPr}
          />
        )}
      </div>
      <div
        id="workspace-panel-terminal"
        role="tabpanel"
        aria-labelledby="workspace-tab-terminal"
        hidden={tab !== 'terminal'}
        className="min-h-0 flex-1 p-3"
      >
        {terminalInitialized ? (
          isArchived ? (
            <p className="p-4 text-sm text-gray-500 dark:text-gray-400">
              {t('sessionArchive.terminal')}
            </p>
          ) : (
            // Keyed by the chat, not the turn: every turn shares the one
            // workspace, and a new turn must not tear its terminals down.
            <TerminalWorkspace
              key={workspaceKey}
              jobId={job.id}
              workspaceKey={workspaceKey}
              active={open && tab === 'terminal'}
              ready={terminalWorkspaceReady}
            />
          )
        ) : null}
      </div>
      <div
        id="workspace-panel-files"
        role="tabpanel"
        aria-labelledby="workspace-tab-files"
        hidden={tab !== 'files'}
        className="min-h-0 flex-1 overflow-y-auto p-5"
      >
        {filesInitialized ? (
          <WorkspaceFiles
            key={workspaceKey}
            job={job}
            active={open && tab === 'files'}
            requestedPath={requestedPath}
            requestToken={requestToken}
          />
        ) : null}
      </div>
      {chats.map((chat) => (
        <div
          key={chat.id}
          id={`workspace-panel-${chat.id}`}
          data-side-chat-id={chat.id}
          role="tabpanel"
          aria-labelledby={`workspace-tab-${chat.id}`}
          hidden={tab !== chat.id}
          className="min-h-0 flex-1"
        >
          <SideChat
            tab={chat}
            sourceJob={job}
            sourceJobs={threadJobs}
            userId={userId}
            config={config}
            active={open && tab === chat.id}
          />
        </div>
      ))}
    </>
  );
}

const WorkspacePanels = memo(WorkspacePanelsView);

export function JobDetailSidePanels({
  panels,
  job,
  userId,
  config,
  events,
  threadJobs,
  terminalWorkspaceReady,
  onReview,
  onWatchPr,
}: {
  panels: JobDetailPanels;
  job: AgentJob;
  userId?: string;
  config: AgentConfigApi | null;
  events: (AgentJobEventApi | AgentThreadEventApi)[];
  threadJobs: AgentJobApi[];
  terminalWorkspaceReady: boolean;
  onReview?: () => void;
  onWatchPr?: () => void;
}) {
  const {
    workspaceOpen,
    workspacePane,
    workspaceScope,
    workspaceInitialized,
    workspaceTab,
    workspaceTabs,
    openWorkspace,
    addSideChat,
    closeSideChat,
    toggleWorkspace,
    recordedFile,
    setRecordedFile,
    gitInitialized,
    terminalInitialized,
    filesInitialized,
    requestedFile,
    inspector,
    inspectorPane,
    trajectorySteps,
    trajectoryQuery,
    setTrajectoryQuery,
    selectInspectorTab,
    selectStep,
    closeInspector,
  } = panels;
  const workspaceJob = useWorkspaceJob(job);
  // The page's actions are new on every render. The retained panels hold
  // these instead, which call the latest and keep one identity while offered.
  const actions = useRef({ onReview, onWatchPr });
  actions.current = { onReview, onWatchPr };
  const review = useCallback(() => actions.current.onReview?.(), []);
  const watchPr = useCallback(() => actions.current.onWatchPr?.(), []);
  const closeRecordedFile = useCallback(() => setRecordedFile(null), [setRecordedFile]);
  const requested = requestedFile?.scope === workspaceScope ? requestedFile : null;
  return (
    <>
      {workspaceOpen ? (
        <PaneResizer
          pane={workspacePane}
          label="Resize workspace"
          controls="job-workspace-pane"
          className="hidden lg:block"
        />
      ) : null}

      <aside
        key={workspaceScope}
        id="job-workspace-pane"
        role="region"
        aria-label="Job workspace"
        hidden={!workspaceOpen}
        style={{ '--job-workspace-width': `${workspacePane.width}px` } as CSSProperties}
        className={`${
          workspaceOpen ? 'flex' : 'hidden'
        } h-full min-h-0 min-w-0 flex-col overflow-hidden bg-white dark:bg-gray-900 lg:w-[var(--job-workspace-width)] lg:shrink-0`}
      >
        {workspaceInitialized ? (
          <WorkspacePanels
            job={workspaceJob}
            userId={userId}
            config={config}
            threadJobs={threadJobs}
            workspaceKey={workspaceScope}
            open={workspaceOpen}
            tab={workspaceTab}
            chats={workspaceTabs.chats}
            gitInitialized={gitInitialized}
            terminalInitialized={terminalInitialized}
            filesInitialized={filesInitialized}
            requestedPath={requested?.path}
            requestToken={requested?.token}
            recordedFile={recordedFile}
            terminalWorkspaceReady={terminalWorkspaceReady}
            onSelect={openWorkspace}
            onAddChat={addSideChat}
            onCloseChat={closeSideChat}
            onBack={toggleWorkspace}
            onCloseRecordedFile={closeRecordedFile}
            onReview={onReview ? review : undefined}
            onWatchPr={onWatchPr ? watchPr : undefined}
          />
        ) : null}
      </aside>

      {inspector ? (
        <PaneResizer
          pane={inspectorPane}
          label="Resize step details"
          controls="job-inspector-pane"
          className="hidden lg:block"
        />
      ) : null}

      <aside
        id="job-inspector-pane"
        role="region"
        aria-label={inspector?.kind === 'run' ? 'Run details' : 'Step details'}
        hidden={!inspector}
        style={{ '--job-inspector-width': `${inspectorPane.width}px` } as CSSProperties}
        className={`${
          inspector ? 'flex' : 'hidden'
        } h-full min-h-0 min-w-0 flex-col overflow-hidden border-l border-gray-200 bg-white dark:border-white/10 dark:bg-gray-900 lg:w-[var(--job-inspector-width)] lg:shrink-0`}
      >
        {inspector ? (
          <TrajectoryInspector
            selection={inspector}
            steps={trajectorySteps}
            events={events}
            jobs={threadJobs}
            currentJob={job}
            query={trajectoryQuery}
            onClearQuery={() => setTrajectoryQuery('')}
            onSelectTab={selectInspectorTab}
            onSelectStep={selectStep}
            onClose={closeInspector}
            onOpenGitDiff={() => openWorkspace('git')}
          />
        ) : null}
      </aside>
    </>
  );
}
