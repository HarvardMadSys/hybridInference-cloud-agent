'use client';

import dynamic from 'next/dynamic';
import { lazy } from 'react';

function PanelLoading({ children }: { children: string }) {
  return (
    <p role="status" className="p-4 text-sm text-gray-500 dark:text-gray-400">
      {children}
    </p>
  );
}

// These panels are optional: loading a conversation should not also load its
// terminal, file browser, side chats, and execution inspectors. Each loading
// view also keeps its suspended import inside the panel in the App Router.
export const TerminalWorkspace = dynamic(
  () => import('../TerminalWorkspace').then((module) => module.TerminalWorkspace),
  { loading: () => <PanelLoading>Loading terminal…</PanelLoading> },
);
export const WorkspaceFiles = dynamic(
  () => import('../WorkspaceFiles').then((module) => module.WorkspaceFiles),
  { loading: () => <PanelLoading>Loading files…</PanelLoading> },
);
export const TrajectoryView = dynamic(
  () => import('../TrajectoryView').then((module) => module.TrajectoryView),
  { loading: () => <PanelLoading>Loading trajectory…</PanelLoading> },
);
export const TrajectoryInspector = dynamic(
  () => import('../TrajectoryInspector').then((module) => module.TrajectoryInspector),
  { loading: () => <PanelLoading>Loading details…</PanelLoading> },
);
export const RecordedDiffPanel = dynamic(
  () => import('../RecordedDiffPanel').then((module) => module.RecordedDiffPanel),
  { loading: () => <PanelLoading>Loading changes…</PanelLoading> },
);
export const SideChat = dynamic(() => import('../SideChat').then((module) => module.SideChat), {
  loading: () => <PanelLoading>Loading side chat…</PanelLoading>,
});
export const SkillSelectionDialog = lazy(() =>
  import('../skills/SkillPicker').then((module) => ({ default: module.SkillSelectionDialog })),
);
