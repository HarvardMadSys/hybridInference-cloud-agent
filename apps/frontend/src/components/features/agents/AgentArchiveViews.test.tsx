// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJob } from './types';

const mocks = vi.hoisted(() => ({
  archive: vi.fn(),
  restore: vi.fn(),
  reload: vi.fn(),
  reloadProjects: vi.fn(),
  replace: vi.fn(),
  useConversationList: vi.fn(),
  useAgentProjects: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => '/job-1',
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ replace: mocks.replace }),
}));

vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: React.ComponentProps<'a'>) => (
    <a href={typeof href === 'string' ? href : ''} {...props}>
      {children}
    </a>
  ),
}));

vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useAuth: () => ({ state: { user: { user_name: 'Test User', email: 'test@example.com' } } }),
    useT: () => (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
      translate('en', key, vars),
  };
});

vi.mock('@/lib/api/agents', () => ({
  archiveAgentJob: mocks.archive,
  restoreAgentJob: mocks.restore,
  getAgentJob: vi.fn().mockRejectedValue(new Error('fixture')),
  getAgentJobThread: vi.fn().mockRejectedValue(new Error('fixture')),
}));

vi.mock('./useConversationList', () => ({ useConversationList: mocks.useConversationList }));

vi.mock('./useAgentJobs', () => ({
  useAgentProjects: mocks.useAgentProjects,
}));

import { AgentsSidebar } from './AgentsSidebar';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';

function job(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'job-1',
    threadId: 'thread-1',
    turnNo: 1,
    createdAt: new Date().toISOString(),
    title: 'Development environment setup',
    state: 'done',
    repo: 'owner/repo',
    baseSha: 'abcdef1',
    branch: 'agent/thread-1',
    runtime: 'claude-code',
    model: 'model',
    spentUsd: 0,
    timeoutLabel: '',
    sandbox: '',
    attempts: [],
    events: [],
    diffFiles: [],
    gates: [],
    usage: { tokensIn: '', tokensOut: '', cachePct: 0, turns: 0 },
    ...overrides,
  };
}

beforeEach(() => {
  mocks.useConversationList.mockReturnValue({
    rows: [
      { key: 'thread-1', job: job(), jobIds: ['job-1'], archived: false, lastActivityAt: null },
    ],
    cursor: null,
    loading: false,
    error: null,
    reload: mocks.reload,
    loadMore: vi.fn(),
  });
  mocks.useAgentProjects.mockReturnValue({
    projects: [{ repo: 'owner/repo', task_count: 1, active_count: 0, last_activity_at: null }],
    loading: false,
    error: null,
    reload: mocks.reloadProjects,
  });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('agent task archiving', () => {
  it('archives a conversation from its sidebar row and leaves an active task', async () => {
    mocks.archive.mockResolvedValue({
      thread_id: 'thread-1',
      archived: true,
      archived_at: new Date().toISOString(),
    });

    render(<AgentsSidebar />);
    expect(mocks.useConversationList).toHaveBeenCalledWith(expect.objectContaining({ refresh: 0 }));
    mocks.useConversationList.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Archive Development environment setup' }));

    await waitFor(() => expect(mocks.archive).toHaveBeenCalledWith('job-1'));
    expect(mocks.reloadProjects).toHaveBeenCalledOnce();
    expect(mocks.useConversationList).toHaveBeenCalledWith(expect.objectContaining({ refresh: 1 }));
    expect(mocks.replace).toHaveBeenCalledWith('/');
    expect(screen.queryByText('Development environment setup')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Archived' })).toHaveAttribute('href', '/archived');
  });

  it('shows a locally hidden sidebar conversation again after it is unarchived elsewhere', async () => {
    mocks.archive.mockResolvedValue({
      thread_id: 'thread-1',
      archived: true,
      archived_at: new Date().toISOString(),
    });

    render(<AgentsSidebar />);
    fireEvent.click(screen.getByRole('button', { name: 'Archive Development environment setup' }));
    await waitFor(() =>
      expect(screen.queryByText('Development environment setup')).not.toBeInTheDocument(),
    );
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-1'], archived: false }));
    expect(await screen.findByText('Development environment setup')).toBeInTheDocument();
  });
});
