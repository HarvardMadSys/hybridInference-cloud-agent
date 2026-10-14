'use client';

import { useT } from '@/components/providers';
import type {
  AgentConfigApi,
  AgentJobApi,
  AgentJobEventApi,
  AgentThreadEventApi,
} from '@/lib/api/agents';
import type { CSSProperties } from 'react';
import { WorkspaceEnvironment } from '../environment/WorkspaceEnvironment';
import { PaneResizer } from '../PaneResizer';
import type { AgentJob } from '../types';
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
  const t = useT();
  const isArchived = Boolean(job.archivedAt);
  const {
    workspaceOpen,
    workspacePane,
    workspaceScope,
    workspaceInitialized,
    workspaceTab,
    workspaceTabs,
    openWorkspace,
    toggleWorkspace,
    recordedFile,
    setRecordedFile,
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
          <>
            <WorkspaceTabBar
              key={`${userId}:${job.threadId ?? job.id}`}
              selected={workspaceTab}
              chats={workspaceTabs.chats}
              diffCount={job.diffFiles.length}
              onSelect={openWorkspace}
              onAdd={workspaceTabs.add}
              onClose={workspaceTabs.close}
              onBack={toggleWorkspace}
            />
            <div
              id="workspace-panel-environment"
              role="tabpanel"
              aria-labelledby="workspace-tab-environment"
              hidden={workspaceTab !== 'environment'}
              className="min-h-0 flex-1 overflow-y-auto"
            >
              {workspaceTab === 'environment' && (
                <WorkspaceEnvironment
                  key={job.threadId ?? job.id}
                  jobId={job.id}
                  repo={job.repo}
                  onTerminal={() => openWorkspace('terminal')}
                />
              )}
            </div>
            <div
              id="workspace-panel-git"
              role="tabpanel"
              aria-labelledby="workspace-tab-git"
              hidden={workspaceTab !== 'git'}
              className="min-h-0 flex-1 overflow-y-auto p-5"
            >
              {recordedFile ? (
                <RecordedDiffPanel
                  selection={recordedFile}
                  turnNo={
                    threadJobs.find((turn) => turn.id === recordedFile.jobId)?.turn_no ??
                    job.turnNo ??
                    1
                  }
                  onClose={() => setRecordedFile(null)}
                />
              ) : (
                <GitPanel
                  job={job}
                  active={workspaceOpen && workspaceTab === 'git'}
                  onReview={onReview}
                  onWatchPr={onWatchPr}
                />
              )}
            </div>
            <div
              id="workspace-panel-terminal"
              role="tabpanel"
              aria-labelledby="workspace-tab-terminal"
              hidden={workspaceTab !== 'terminal'}
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
                    key={workspaceScope}
                    jobId={job.id}
                    workspaceKey={workspaceScope}
                    active={workspaceOpen && workspaceTab === 'terminal'}
                    ready={terminalWorkspaceReady}
                  />
                )
              ) : null}
            </div>
            <div
              id="workspace-panel-files"
              role="tabpanel"
              aria-labelledby="workspace-tab-files"
              hidden={workspaceTab !== 'files'}
              className="min-h-0 flex-1 overflow-y-auto p-5"
            >
              {filesInitialized ? (
                <WorkspaceFiles
                  key={workspaceScope}
                  job={job}
                  active={workspaceOpen && workspaceTab === 'files'}
                  requestedPath={
                    requestedFile?.scope === workspaceScope ? requestedFile.path : undefined
                  }
                  requestToken={
                    requestedFile?.scope === workspaceScope ? requestedFile.token : undefined
                  }
                />
              ) : null}
            </div>
            {workspaceTabs.chats.map((chat) => (
              <div
                key={chat.id}
                id={`workspace-panel-${chat.id}`}
                data-side-chat-id={chat.id}
                role="tabpanel"
                aria-labelledby={`workspace-tab-${chat.id}`}
                hidden={workspaceTab !== chat.id}
                className="min-h-0 flex-1"
              >
                <SideChat
                  tab={chat}
                  sourceJob={job}
                  sourceJobs={threadJobs}
                  userId={userId}
                  config={config}
                  active={workspaceOpen && workspaceTab === chat.id}
                />
              </div>
            ))}
          </>
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
