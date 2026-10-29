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
import { buildTrajectorySteps, turnContextFromJobs, type TrajectoryStep } from '../trajectorySteps';
import type { AgentEvent, AgentJob } from '../types';
import { useResizablePane } from '../useResizablePane';
import { useWorkspaceTabs, type SideChatTab, type WorkspaceTab } from '../useWorkspaceTabs';
import { opensContext } from '../context/contextPresent';
import { useKeyboardShortcuts } from '@/components/features/settings/keyboardShortcutPreferences';
import { KEYBOARD_INPUT_SELECTOR, matchesShortcut } from '@/lib/keyboardShortcuts';

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

/** The conversation's main views: its chat, its trajectory of steps, and its context. */
export type ConversationView = 'chat' | 'trajectory' | 'context';

/** A delegated agent, named by the turn that started it and its overview key. */
export interface SubagentTarget {
  jobId: string;
  key: string;
}

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
  const [shortcuts] = useKeyboardShortcuts(userId);
  const workspaceScope = JSON.stringify([userId, job.threadId ?? job.id]);
  const [tab, setTab] = useState<ConversationView>('chat');
  // Context insights link a conversation straight to its Context view.
  useEffect(() => {
    if (opensContext(window.location.search)) setTab('context');
  }, []);
  // The subagent being read, if any. Chat shows its own conversation and the
  // trajectory its steps; without one, both show the whole conversation.
  const [subagent, setSubagent] = useState<SubagentTarget | null>(null);
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
  // Git included: a large diff is the heaviest thing the workspace draws, and a
  // workspace opened on another tab has no reason to draw it.
  const gitInitialized =
    (workspaceOpen && workspaceTab === 'git') ||
    (initializedWorkspace?.scope === workspaceScope && initializedWorkspace.tabs.has('git'));
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

  // Every Markdown link in the transcript and every retained workspace panel
  // holds these actions. They keep one identity and read the latest state
  // through this ref, so a keystroke or a streamed event does not draw all of
  // those again just to hand them the same action.
  const latest = useRef({ workspaceOpen, workspaceScope, workspaceTabs, setWorkspaceOpen });
  latest.current = { workspaceOpen, workspaceScope, workspaceTabs, setWorkspaceOpen };

  const openWorkspace = useCallback((tab: WorkspaceTab) => {
    const { workspaceOpen, workspaceTabs, setWorkspaceOpen } = latest.current;
    if (!workspaceOpen) {
      const active = document.activeElement;
      workspaceTriggerRef.current = active instanceof HTMLElement ? active : null;
    }
    focusWorkspaceRef.current = true;
    setRecordedFile(null);
    workspaceTabs.select(tab);
    setWorkspaceOpen(true);
    setInspector(null);
  }, []);

  const openWorkspaceFile = useCallback(
    (path: string) => {
      const scope = latest.current.workspaceScope;
      setRequestedFile((current) => ({ scope, path, token: (current?.token ?? 0) + 1 }));
      openWorkspace('files');
    },
    [openWorkspace],
  );

  const openRecordedFile = useCallback(
    (selection: RecordedFileSelection) => {
      openWorkspace('git');
      setRecordedFile({ scope: latest.current.workspaceScope, selection });
    },
    [openWorkspace],
  );

  // The side chat tab actions, with one identity for the memoized workspace panels.
  const addSideChat = useCallback(() => latest.current.workspaceTabs.add(), []);
  const closeSideChat = useCallback(
    (id: SideChatTab['id']) => latest.current.workspaceTabs.close(id),
    [],
  );

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

  // The Context view names a request by its stored event. Its step opens when
  // this page has read that event; otherwise the turn it belongs to does.
  const openContextPoint = useCallback(
    ({ jobId, turnNo, eventId }: { jobId: string; turnNo: number; eventId: number | null }) => {
      const holds = (candidate: TrajectoryStep) =>
        candidate.jobId === jobId && candidate.sourceEvents.some((event) => event.id === eventId);
      const step = eventId === null ? undefined : getTrajectorySteps().find(holds);
      if (!step) {
        openRun(jobId, turnNo);
        return;
      }
      setTab('trajectory');
      selectStep(step.key);
    },
    [getTrajectorySteps, openRun, selectStep],
  );

  // `/context` and the panel's own tab open the context beside the chat, so
  // the conversation can still be read and answered. From the panel, the
  // full view takes the main area, and the panel steps aside for it.
  const openContextPanel = useCallback(() => openWorkspace('context'), [openWorkspace]);
  const openContextTab = useCallback(() => {
    setTab('context');
    setWorkspaceOpen(false);
  }, [setWorkspaceOpen]);

  // The trajectory's subagent filter, as its menu value: `[jobId, key]` or empty.
  const setSubagentFilter = useCallback((value: string) => {
    if (!value) {
      setSubagent(null);
      return;
    }
    const [jobId, key] = JSON.parse(value) as [string, string];
    setSubagent({ jobId, key });
  }, []);

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
    if (tab !== 'chat') return;
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
          `${KEYBOARD_INPUT_SELECTOR}, [role="dialog"], [role="menu"], #job-workspace-pane`,
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
      if (!matchesShortcut(event, shortcuts.workspace)) return;
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat) toggleWorkspace();
    };
    // Capture before terminal/editor handlers consume the chord or send Ctrl+J to the shell.
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [toggleWorkspace, shortcuts.workspace]);

  return {
    /** The turn the panels were opened for. */
    job,
    workspaceShortcut: shortcuts.workspace,
    workspaceScope,
    tab,
    setTab,
    subagent,
    setSubagent,
    setSubagentFilter,
    workspaceOpen,
    setWorkspaceOpen,
    workspaceTabs,
    workspaceTab,
    workspaceInitialized,
    gitInitialized,
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
    addSideChat,
    closeSideChat,
    toggleWorkspace,
    closeInspector,
    selectStep,
    openContextPoint,
    openContextPanel,
    openContextTab,
    inspectTranscriptEvent,
    openRun,
    selectInspectorTab,
  };
}

export type JobDetailPanels = ReturnType<typeof useJobDetailPanels>;
