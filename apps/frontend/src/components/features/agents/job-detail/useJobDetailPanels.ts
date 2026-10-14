'use client';

import type { AgentJobApi, AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { RecordedFileSelection } from '../recordedTurnOutputs';
import {
  normalizeStepTab,
  type InspectorSelection,
  type RunTab,
  type StepTab,
} from '../trajectoryInspectorTabs';
import { buildTrajectorySteps, turnContextFromJobs } from '../trajectorySteps';
import type { AgentEvent, AgentJob } from '../types';
import { useResizablePane } from '../useResizablePane';
import { useWorkspaceTabs, type WorkspaceTab } from '../useWorkspaceTabs';

export const WORKSPACE_WIDTH_STORAGE_KEY = 'agents.workspace.width';
const WORKSPACE_DEFAULT_WIDTH = 560;
const WORKSPACE_MIN_WIDTH = 360;
const WORKSPACE_MAX_WIDTH = 1200;
// The transcript is the reason the page exists; the workspace stops widening
// before the conversation and its composer get squeezed into a column.
const TRANSCRIPT_MIN_WIDTH = 400;
// The inspector is narrower than the workspace: it holds one step's detail,
// not a terminal. It still has to leave the trajectory list enough room to scan.
export const INSPECTOR_WIDTH_STORAGE_KEY = 'agents.inspector.width';
const INSPECTOR_DEFAULT_WIDTH = 400;
const INSPECTOR_MIN_WIDTH = 320;
const INSPECTOR_MAX_WIDTH = 640;
const TRAJECTORY_MIN_WIDTH = 420;

const EMPTY_TRAJECTORY_STEPS: ReturnType<typeof buildTrajectorySteps> = [];

/** Owns the mutually exclusive workspace and inspector, their focus, and retained panels. */
export function useJobDetailPanels({
  job,
  userId,
  events,
  threadJobs,
}: {
  job: AgentJob;
  userId?: string;
  events: (AgentJobEventApi | AgentThreadEventApi)[];
  threadJobs: AgentJobApi[];
}) {
  const workspaceScope = JSON.stringify([userId, job.threadId ?? job.id]);
  const [tab, setTab] = useState<'chat' | 'trajectory'>('chat');
  // A subagent's own conversation replaces the Chat body; it is a Chat view.
  const [subagentKey, setSubagentKey] = useState<string | null>(null);
  const [openWorkspaceScope, setOpenWorkspaceScope] = useState<string | null>(null);
  const workspaceOpen = openWorkspaceScope === workspaceScope;
  const setWorkspaceOpen = useCallback(
    (open: boolean) => setOpenWorkspaceScope(open ? workspaceScope : null),
    [workspaceScope],
  );
  const [initializedWorkspace, setInitializedWorkspace] = useState<{
    scope: string;
    tabs: ReadonlySet<WorkspaceTab>;
  } | null>(null);
  useEffect(() => {
    setOpenWorkspaceScope(null);
    setInitializedWorkspace(null);
  }, [workspaceScope]);
  const workspaceTriggerRef = useRef<HTMLElement | null>(null);
  const focusWorkspaceRef = useRef(false);
  const [activityOpen, setActivityOpen] = useState(true);
  const [wideLayout, setWideLayout] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(min-width: 1024px)');
    const sync = () => setWideLayout(query.matches);
    sync();
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  }, []);
  const activityToggleRef = useRef<HTMLButtonElement>(null);
  const workspaceTabs = useWorkspaceTabs(userId, job, threadJobs);
  const workspaceTab = workspaceTabs.selected;
  // Mount a panel only when first used, then retain it while hidden so closing
  // the workspace preserves terminals, file previews, and side chat drafts.
  const workspaceInitialized = workspaceOpen || initializedWorkspace?.scope === workspaceScope;
  const terminalInitialized =
    (workspaceOpen && workspaceTab === 'terminal') ||
    (initializedWorkspace?.scope === workspaceScope && initializedWorkspace.tabs.has('terminal'));
  const filesInitialized =
    (workspaceOpen && workspaceTab === 'files') ||
    (initializedWorkspace?.scope === workspaceScope && initializedWorkspace.tabs.has('files'));
  useEffect(() => {
    if (!workspaceOpen) return;
    setInitializedWorkspace((current) => {
      if (current?.scope === workspaceScope && current.tabs.has(workspaceTab)) return current;
      const tabs = new Set(current?.scope === workspaceScope ? current.tabs : []);
      tabs.add(workspaceTab);
      return { scope: workspaceScope, tabs };
    });
  }, [workspaceOpen, workspaceScope, workspaceTab]);
  // The workspace and the step inspector take turns in the same right region.
  const [inspector, setInspector] = useState<InspectorSelection | null>(null);
  const focusInspectorRef = useRef(false);
  const [trajectoryQuery, setTrajectoryQuery] = useState('');
  const [focusJobId, setFocusJobId] = useState<string | null>(null);
  const inspectorTriggerRef = useRef<HTMLElement | null>(null);
  const trajectoryContext = useMemo(() => {
    const context = turnContextFromJobs(threadJobs);
    if (
      !context.inputs?.some((input) => input.jobId === job.id) &&
      typeof job.prompt === 'string'
    ) {
      context.inputs = [
        ...(context.inputs ?? []),
        {
          jobId: job.id,
          turnNo: job.turnNo ?? 1,
          text: job.prompt,
          at: job.createdAt ?? null,
        },
      ];
    }
    return context;
  }, [threadJobs, job.id, job.prompt, job.turnNo, job.createdAt]);
  // The diagnostic projection can be large. Keep a getter for transcript
  // clicks, and construct it only while a reader uses the trajectory/details.
  const getTrajectorySteps = useMemo(() => {
    let steps: ReturnType<typeof buildTrajectorySteps> | undefined;
    return () => {
      steps ??= buildTrajectorySteps(events, trajectoryContext);
      return steps;
    };
  }, [events, trajectoryContext]);
  const trajectorySteps =
    tab === 'trajectory' || inspector !== null ? getTrajectorySteps() : EMPTY_TRAJECTORY_STEPS;
  const [requestedFile, setRequestedFile] = useState<{
    scope: string;
    path: string;
    token: number;
  } | null>(null);
  const [recordedSelection, setRecordedFile] = useState<{
    scope: string;
    selection: RecordedFileSelection;
  } | null>(null);
  const recordedFile =
    recordedSelection?.scope === workspaceScope ? recordedSelection.selection : null;
  const workspacePane = useResizablePane({
    storageKey: WORKSPACE_WIDTH_STORAGE_KEY,
    defaultWidth: WORKSPACE_DEFAULT_WIDTH,
    minWidth: WORKSPACE_MIN_WIDTH,
    maxWidth: WORKSPACE_MAX_WIDTH,
    side: 'end',
    maxViewportFraction: 0.75,
    siblingMinWidth: TRANSCRIPT_MIN_WIDTH,
  });
  const inspectorPane = useResizablePane({
    storageKey: INSPECTOR_WIDTH_STORAGE_KEY,
    defaultWidth: INSPECTOR_DEFAULT_WIDTH,
    minWidth: INSPECTOR_MIN_WIDTH,
    maxWidth: INSPECTOR_MAX_WIDTH,
    side: 'end',
    maxViewportFraction: 0.6,
    siblingMinWidth: TRAJECTORY_MIN_WIDTH,
  });
  // Measure every pane against the space remaining after the task sidebar.
  const { containerRef: workspaceContainerRef } = workspacePane;
  const { containerRef: inspectorContainerRef } = inspectorPane;
  const layoutContainerRef = useCallback(
    (node: HTMLElement | null) => {
      workspaceContainerRef(node);
      inspectorContainerRef(node);
    },
    [inspectorContainerRef, workspaceContainerRef],
  );

  function openWorkspace(tab: WorkspaceTab) {
    if (!workspaceOpen) {
      const active = document.activeElement;
      workspaceTriggerRef.current = active instanceof HTMLElement ? active : null;
    }
    focusWorkspaceRef.current = true;
    setRecordedFile(null);
    workspaceTabs.select(tab);
    setWorkspaceOpen(true);
    setInspector(null);
  }

  function openWorkspaceFile(path: string) {
    setRequestedFile((current) => ({
      scope: workspaceScope,
      path,
      token: (current?.token ?? 0) + 1,
    }));
    openWorkspace('files');
  }

  function openRecordedFile(selection: RecordedFileSelection) {
    openWorkspace('git');
    setRecordedFile({ scope: workspaceScope, selection });
  }

  const toggleWorkspace = useCallback(() => {
    if (workspaceOpen) {
      setWorkspaceOpen(false);
      const trigger = workspaceTriggerRef.current;
      workspaceTriggerRef.current = null;
      window.requestAnimationFrame(() => {
        if (trigger && document.contains(trigger) && !trigger.closest('[hidden]')) trigger.focus();
        else
          document
            .querySelector<HTMLButtonElement>('button[aria-controls="job-workspace-pane"]')
            ?.focus();
      });
    } else {
      const active = document.activeElement;
      workspaceTriggerRef.current = active instanceof HTMLElement ? active : null;
      focusWorkspaceRef.current = true;
      setInspector(null);
      setWorkspaceOpen(true);
    }
  }, [workspaceOpen, setWorkspaceOpen]);

  useEffect(() => {
    if (!workspaceOpen || !focusWorkspaceRef.current) return;
    focusWorkspaceRef.current = false;
    document.getElementById(`workspace-tab-${workspaceTab}`)?.focus();
  }, [workspaceOpen, workspaceTab, requestedFile?.token, recordedFile]);

  const closeInspector = useCallback(() => {
    setInspector(null);
    setFocusJobId(null);
    const trigger = inspectorTriggerRef.current;
    inspectorTriggerRef.current = null;
    // The transcript becomes visible in the next commit on narrow screens.
    window.requestAnimationFrame(() => {
      if (trigger && document.contains(trigger)) trigger.focus();
      else if (trigger) activityToggleRef.current?.focus();
    });
  }, []);

  // Remember what opened the panel so closing it can hand focus back.
  const rememberInspectorTrigger = useCallback(() => {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement && active.closest('#job-inspector-pane'))) {
      inspectorTriggerRef.current = active instanceof HTMLElement ? active : null;
    }
    focusInspectorRef.current = true;
  }, []);

  useEffect(() => {
    if (!inspector || !focusInspectorRef.current) return;
    focusInspectorRef.current = false;
    document
      .querySelector<HTMLButtonElement>('#job-inspector-pane button[aria-label="Close details"]')
      ?.focus();
  }, [inspector]);

  const selectStep = useCallback(
    (stepKey: string) => {
      const step = getTrajectorySteps().find((candidate) => candidate.key === stepKey);
      rememberInspectorTrigger();
      setWorkspaceOpen(false);
      setInspector((current) => {
        const keepTab = current?.kind === 'step' && current.stepKey === stepKey;
        const tab: StepTab = keepTab ? current.tab : 'summary';
        return { kind: 'step', stepKey, tab: step ? normalizeStepTab(step, tab) : 'summary' };
      });
    },
    [rememberInspectorTrigger, setWorkspaceOpen, getTrajectorySteps],
  );

  const inspectTranscriptEvent = useCallback(
    (event: AgentEvent, eventJobId: string) => {
      const step = getTrajectorySteps().find(
        (candidate) =>
          candidate.jobId === eventJobId &&
          (candidate.sourceEvents.some((source) => source.id === event.sourceEventId) ||
            (event.kind === 'tool_use' &&
              event.id &&
              candidate.callId === event.id &&
              candidate.attemptId ===
                threadJobs.find((turn) => turn.id === eventJobId)?.current_attempt_id)),
      );
      if (step) selectStep(step.key);
    },
    [selectStep, getTrajectorySteps, threadJobs],
  );

  const openRun = useCallback(
    (jobId: string, turnNo: number) => {
      rememberInspectorTrigger();
      setWorkspaceOpen(false);
      setInspector({ kind: 'run', jobId, turnNo, tab: 'overview' });
      setTab('trajectory');
      setFocusJobId(jobId);
    },
    [rememberInspectorTrigger, setWorkspaceOpen],
  );

  const selectInspectorTab = useCallback((next: StepTab | RunTab) => {
    setInspector((current) => {
      if (!current) return current;
      return current.kind === 'step'
        ? { ...current, tab: next as StepTab }
        : { ...current, tab: next as RunTab };
    });
  }, []);

  // Returning to Chat closes details selected from the trajectory.
  useEffect(() => {
    if (tab !== 'chat') {
      setSubagentKey(null);
      return;
    }
    setInspector(null);
    setFocusJobId(null);
  }, [tab]);

  // Escape closes this layer only. Controls that consume Escape themselves —
  // the composer, the terminal, any dialog — keep it.
  useEffect(() => {
    if (!inspector) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.repeat || event.defaultPrevented) return;
      const target = event.target instanceof Element ? event.target : null;
      if (
        target?.closest(
          'input, textarea, select, [contenteditable="true"], [role="dialog"], [role="menu"], #job-workspace-pane',
        )
      ) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      closeInspector();
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [closeInspector, inspector]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('[role="dialog"]')) return;
      const hasOnePrimaryModifier = event.metaKey !== event.ctrlKey;
      if (
        event.key.toLowerCase() !== 'j' ||
        !hasOnePrimaryModifier ||
        event.altKey ||
        event.shiftKey
      ) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat) toggleWorkspace();
    };
    // Capture before terminal/editor handlers consume the chord or send Ctrl+J to the shell.
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [toggleWorkspace]);

  return {
    workspaceScope,
    tab,
    setTab,
    subagentKey,
    setSubagentKey,
    workspaceOpen,
    setWorkspaceOpen,
    workspaceTabs,
    workspaceTab,
    workspaceInitialized,
    terminalInitialized,
    filesInitialized,
    wideLayout,
    activityOpen,
    setActivityOpen,
    activityToggleRef,
    inspector,
    setInspector,
    focusJobId,
    setFocusJobId,
    trajectoryQuery,
    setTrajectoryQuery,
    trajectoryContext,
    getTrajectorySteps,
    trajectorySteps,
    requestedFile,
    recordedFile,
    setRecordedFile,
    workspacePane,
    inspectorPane,
    layoutContainerRef,
    openWorkspace,
    openWorkspaceFile,
    openRecordedFile,
    toggleWorkspace,
    closeInspector,
    selectStep,
    inspectTranscriptEvent,
    openRun,
    selectInspectorTab,
  };
}

export type JobDetailPanels = ReturnType<typeof useJobDetailPanels>;
