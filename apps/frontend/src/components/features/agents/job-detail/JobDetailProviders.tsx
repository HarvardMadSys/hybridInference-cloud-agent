'use client';

import type { ReactNode } from 'react';
import { WorkspaceLinkProvider } from '@/components/ui/Markdown';
import { ThreadContextProvider } from '../context/threadContextData';
import type { JobDetailPanels } from './useJobDetailPanels';

/**
 * What the conversation page hands everything inside it: transcript links
 * open workspace files, and one read of the conversation's context serves
 * the Context tab, the workspace panel and `/context`, which opens the panel.
 */
export function JobDetailProviders({
  panels,
  children,
}: {
  panels: Pick<JobDetailPanels, 'job' | 'openWorkspaceFile' | 'openContextPanel'>;
  children: ReactNode;
}) {
  return (
    <WorkspaceLinkProvider onOpenFile={panels.openWorkspaceFile}>
      <ThreadContextProvider job={panels.job} onOpen={panels.openContextPanel}>
        {children}
      </ThreadContextProvider>
    </WorkspaceLinkProvider>
  );
}
