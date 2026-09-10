// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJobApi, AgentSessionApi, AgentSessionListOptions } from '@/lib/api/agents';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  setArchived: vi.fn(),
  deleteSessions: vi.fn(),
  locale: 'en' as 'en' | 'zh-CN',
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
  const t = (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
    translate(mocks.locale, key, vars);
  return { useT: () => t, useLocale: () => ({ locale: mocks.locale, t }) };
});

vi.mock('@/lib/api/agents', () => ({
  listAgentSessions: mocks.list,
  setAgentSessionsArchived: mocks.setArchived,
  deleteAgentSessions: mocks.deleteSessions,
}));

import { ArchivedTasksView } from './ArchivedTasksView';

function session(
  id: string,
  overrides: Partial<AgentSessionApi> = {},
  job: Partial<AgentJobApi> = {},
): AgentSessionApi {
  return {
    thread_id: `thread-${id}`,
    title: `Session ${id}`,
    archived_at: '2026-09-09T16:27:00Z',
    can_delete: true,
    job: {
      id: `job-${id}`,
      repo: 'owner/alpha',
      task_prompt: `Session ${id}`,
      runtime: 'codex',
      model: 'model',
      state: 'succeeded',
      published_pr_url: 'https://github.com/owner/alpha/pull/1',
      ...job,
    } as AgentJobApi,
    ...overrides,
  };
}

let sessions: AgentSessionApi[];

function listing(options: AgentSessionListOptions = {}) {
  const matches = sessions.filter(
    (item) =>
      Boolean(item.archived_at) === (options.archived ?? true) &&
      (!options.search || item.title.toLowerCase().includes(options.search.toLowerCase())) &&
      (!options.state || item.job.state === options.state),
  );
  const facets = new Map<string, number>();
  for (const item of matches) facets.set(item.job.repo, (facets.get(item.job.repo) ?? 0) + 1);
  const filtered = matches.filter((item) => !options.repo || item.job.repo === options.repo);
  const start = Number(options.cursor ?? '0');
  const end = start + (options.limit ?? 50);
  return {
    sessions: filtered.slice(start, end),
    total: filtered.length,
    next_cursor: end < filtered.length ? String(end) : null,
    projects: [...facets].map(([repo, count]) => ({ repo, count })),
  };
}

beforeEach(() => {
  mocks.locale = 'en';
  sessions = [session('one')];
  mocks.list.mockImplementation(async (options: AgentSessionListOptions) => listing(options));
  mocks.setArchived.mockImplementation(async (ids: string[], archived: boolean) => {
    sessions = sessions.map((item) =>
      ids.includes(item.thread_id)
        ? { ...item, archived_at: archived ? '2026-09-10T12:00:00Z' : null }
        : item,
    );
    announceAgentSessionsChanged({ threadIds: ids, archived });
    return {
      sessions: ids.map((thread_id) => ({
        thread_id,
        archived,
        archived_at: archived ? '2026-09-10T12:00:00Z' : null,
      })),
    };
  });
  mocks.deleteSessions.mockImplementation(async (ids: string[]) => {
    sessions = sessions.filter((item) => !ids.includes(item.thread_id));
    announceAgentSessionsChanged({ threadIds: ids, deleted: true });
    return { thread_ids: ids };
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe('archived session management', () => {
  it('opens archived history without unarchiving and keeps running status visible', async () => {
    sessions = [session('running', { can_delete: false }, { state: 'running' })];
    render(<ArchivedTasksView />);
    const title = await screen.findByRole('link', { name: /Session running/ });
    expect(title).toHaveAttribute('href', '/job-running');
    expect(within(title).getByText('Running')).toBeVisible();
    expect(title.querySelector('time')).toHaveAttribute('datetime', '2026-09-09T16:27:00Z');
    expect(screen.getByRole('button', { name: 'Delete Session running' })).toBeDisabled();
    expect(mocks.setArchived).not.toHaveBeenCalled();
  });

  it('unarchives a whole session, offers its history link, and undoes the change', async () => {
    render(<ArchivedTasksView />);
    fireEvent.click(await screen.findByRole('button', { name: 'Unarchive Session one' }));
    await waitFor(() => expect(mocks.setArchived).toHaveBeenCalledWith(['thread-one'], false));
    expect(await screen.findByText('1 session returned to its original project')).toBeVisible();
    expect(screen.queryByRole('link', { name: /Session one/ })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View' })).toHaveAttribute('href', '/job-one');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(await screen.findByRole('link', { name: /Session one/ })).toBeVisible();
    expect(mocks.setArchived).toHaveBeenLastCalledWith(['thread-one'], true);
  });

  it('keeps complete group counts, loaded rows, and collapsed groups after a mutation', async () => {
    sessions = [
      ...Array.from({ length: 55 }, (_, index) => session(String(index))),
      session('beta', {}, { repo: 'other/beta' }),
    ];
    render(<ArchivedTasksView />);
    const alpha = await screen.findByRole('region', { name: 'owner/alpha' });
    await within(alpha).findByRole('link', { name: /Session 4 / });
    expect(within(alpha).getByText('55 sessions')).toBeVisible();
    expect(screen.getByRole('combobox', { name: 'Filter by project' })).toContainHTML('other/beta');
    fireEvent.click(
      within(alpha).getByRole('button', { name: 'Show more sessions (50 remaining)' }),
    );
    expect(await within(alpha).findByRole('link', { name: /Session 9 / })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'beta' }));
    fireEvent.click(within(alpha).getByRole('button', { name: 'Unarchive Session 0' }));
    await waitFor(() => expect(within(alpha).getByText('54 sessions')).toBeVisible());
    expect(screen.getByRole('button', { name: 'beta' })).toHaveAttribute('aria-expanded', 'false');
    expect(await within(alpha).findByRole('link', { name: /Session 9 / })).toBeVisible();
    expect(await within(alpha).findByRole('link', { name: /Session 10 / })).toBeVisible();
    expect(within(alpha).getAllByRole('link')).toHaveLength(10);
  });

  it('applies title, status, and repository filters on the server, including old history', async () => {
    sessions = [
      session('latest'),
      session(
        'old',
        { title: 'Old timeout investigation' },
        { state: 'failed', repo: 'other/beta' },
      ),
    ];
    render(<ArchivedTasksView />);
    await screen.findByRole('link', { name: /Session latest/ });
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search archived sessions' }), {
      target: { value: 'timeout' },
    });
    await waitFor(() =>
      expect(mocks.list).toHaveBeenCalledWith(
        expect.objectContaining({ search: 'timeout', limit: 1 }),
        expect.any(AbortSignal),
      ),
    );
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter by status' }), {
      target: { value: 'failed' },
    });
    await screen.findByRole('link', { name: /Old timeout investigation/ });
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter by project' }), {
      target: { value: 'other/beta' },
    });
    await waitFor(() =>
      expect(mocks.list).toHaveBeenCalledWith(
        expect.objectContaining({
          search: 'timeout',
          state: 'failed',
          repo: 'other/beta',
          limit: 1,
        }),
        expect.any(AbortSignal),
      ),
    );
    expect(await screen.findByRole('link', { name: /Old timeout investigation/ })).toBeVisible();
    expect(screen.queryByRole('link', { name: /Session latest/ })).not.toBeInTheDocument();
  });

  it('prepares every filtered page, excludes busy sessions, and deletes only the confirmed IDs', async () => {
    sessions = [
      ...Array.from({ length: 102 }, (_, index) =>
        session(
          String(index),
          { title: `Maintenance ${index}`, can_delete: index !== 0 && index !== 101 },
          { state: index === 0 || index === 101 ? 'running' : 'succeeded' },
        ),
      ),
      session('unrelated', {}, { repo: 'other/beta' }),
    ];
    render(<ArchivedTasksView />);
    await screen.findByRole('link', { name: /Maintenance 1 / });
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter by project' }), {
      target: { value: 'owner/alpha' },
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Delete filtered' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Delete filtered' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete 100 archived sessions?' });
    expect(within(dialog).getByText('Scope: current search and filters')).toBeVisible();
    expect(within(dialog).getByText(/2 active sessions are excluded/)).toBeVisible();
    expect(mocks.list).toHaveBeenCalledWith(
      expect.objectContaining({ repo: 'owner/alpha', limit: 100, cursor: '100' }),
      expect.any(AbortSignal),
    );
    expect(mocks.deleteSessions).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete 100 sessions' }));
    await waitFor(() => expect(mocks.deleteSessions).toHaveBeenCalledOnce());
    const deleted = mocks.deleteSessions.mock.calls[0][0] as string[];
    expect(deleted).toHaveLength(100);
    expect(deleted).toContain('thread-100');
    expect(deleted).not.toContain('thread-0');
    expect(deleted).not.toContain('thread-101');
    expect(deleted).not.toContain('thread-unrelated');
    expect(await screen.findByText('100 sessions deleted')).toBeVisible();
  });

  it('keeps delete confirmation scoped to a group and supports keyboard cancellation', async () => {
    sessions = [session('one'), session('two', {}, { repo: 'other/beta' })];
    render(<ArchivedTasksView />);
    fireEvent.click(await screen.findByRole('button', { name: 'Actions for owner/alpha' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete group (1)' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Scope: archived sessions in owner/alpha')).toBeVisible();
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mocks.deleteSessions).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Actions for owner/alpha' })).toHaveFocus();
  });

  it('blocks oversized snapshots and explains how to narrow the selection', async () => {
    sessions = Array.from({ length: 1001 }, (_, index) => session(String(index)));
    render(<ArchivedTasksView />);
    await screen.findByRole('link', { name: /Session 0 / });
    fireEvent.click(screen.getByRole('button', { name: 'Delete all' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete 1001 archived sessions?' });
    expect(within(dialog).getByRole('button', { name: 'Delete 1001 sessions' })).toBeDisabled();
    expect(within(dialog).getByText(/Select at most 1000 sessions/)).toBeVisible();
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeEnabled();
    expect(mocks.deleteSessions).not.toHaveBeenCalled();
  });

  it('shows a retryable list failure and retains rows when deletion fails', async () => {
    mocks.list.mockRejectedValueOnce(new Error('connection unavailable'));
    render(<ArchivedTasksView />);
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Session one' }));
    const dialog = await screen.findByRole('dialog');
    mocks.deleteSessions.mockRejectedValueOnce(new Error('409 session became active'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete 1 session' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Could not delete these sessions',
    );
    expect(screen.getByRole('link', { name: /Session one/ })).toBeVisible();
  });

  it('localizes the archive UI and empty search state', async () => {
    mocks.locale = 'zh-CN';
    render(<ArchivedTasksView />);
    await screen.findByRole('button', { name: '取消归档 Session one' });
    fireEvent.change(screen.getByRole('searchbox', { name: '搜索已归档会话' }), {
      target: { value: '不存在的会话' },
    });
    expect(await screen.findByText('没有找到匹配的会话')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '清除筛选' }));
    expect(await screen.findByRole('button', { name: '取消归档 Session one' })).toBeVisible();
  });

  it('ignores a stale response after a session-change refresh', async () => {
    let finishStale: ((value: ReturnType<typeof listing>) => void) | undefined;
    const stale = listing({ limit: 1 });
    mocks.list.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishStale = resolve;
        }),
    );
    render(<ArchivedTasksView />);
    sessions = [];
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-one'], archived: false }));
    expect(await screen.findByText('All caught up')).toBeVisible();
    await act(async () => finishStale?.(stale));
    expect(screen.getByText('All caught up')).toBeVisible();
    expect(screen.queryByRole('link', { name: /Session one/ })).not.toBeInTheDocument();
  });

  it('refreshes running sessions and stops polling after their work settles', async () => {
    vi.useFakeTimers();
    sessions = [session('running', { can_delete: false }, { state: 'running' })];
    await act(async () => {
      render(<ArchivedTasksView />);
    });
    expect(screen.getByRole('button', { name: 'Delete Session running' })).toBeDisabled();
    sessions = [session('running')];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(screen.getByRole('button', { name: 'Delete Session running' })).toBeEnabled();
    expect(
      within(screen.getByRole('link', { name: /Session running/ })).queryByText('Running'),
    ).not.toBeInTheDocument();
    const calls = mocks.list.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(mocks.list).toHaveBeenCalledTimes(calls);
  });

  it('refreshes filtered totals when the last running session finishes', async () => {
    vi.useFakeTimers();
    sessions = [session('running', { can_delete: false }, { state: 'running' })];
    await act(async () => {
      render(<ArchivedTasksView />);
    });
    await act(async () => {
      fireEvent.change(screen.getByRole('combobox', { name: 'Filter by status' }), {
        target: { value: 'running' },
      });
    });
    expect(screen.getByRole('link', { name: /Session running/ })).toBeVisible();
    sessions = [session('running')];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(screen.getByText('No matching sessions')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Delete filtered' })).toBeDisabled();
  });

  it('keeps existing groups mounted when a background index refresh fails', async () => {
    sessions = [session('one'), session('beta', {}, { repo: 'other/beta' })];
    render(<ArchivedTasksView />);
    await screen.findByRole('link', { name: /Session one/ });
    fireEvent.click(screen.getByRole('button', { name: 'beta' }));
    mocks.list.mockImplementation(async (options: AgentSessionListOptions) => {
      if (options.limit === 1) throw new Error('index temporarily unavailable');
      return listing(options);
    });
    act(() => announceAgentSessionsChanged({ threadIds: [], archived: true }));
    expect(
      await screen.findByText(
        'Could not refresh the archive. Displayed counts may be out of date.',
      ),
    ).toBeVisible();
    expect(screen.getByRole('link', { name: /Session one/ })).toBeVisible();
    expect(screen.getByRole('button', { name: 'beta' })).toHaveAttribute('aria-expanded', 'false');
  });
});
