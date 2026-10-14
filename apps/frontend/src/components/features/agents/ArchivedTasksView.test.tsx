// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJobApi, AgentSessionApi, AgentSessionListOptions } from '@/lib/api/agents';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { APIError } from '@/lib/utils/errors';
import { isTemporaryWorkspace } from '@/lib/temporaryWorkspace';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  setArchived: vi.fn(),
  deleteSessions: vi.fn(),
  review: vi.fn(),
  locale: 'en' as 'en' | 'zh-CN',
}));

vi.mock('./useSessionReview', () => ({ useSessionReview: mocks.review }));

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

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
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
const intersections = new Map<Element, (visible: boolean) => void>();

async function approachBoundary(container: HTMLElement) {
  const boundary = [...intersections.keys()].find((element) => container.contains(element));
  expect(boundary).toBeDefined();
  await act(async () => intersections.get(boundary!)!(true));
}

function listing(options: AgentSessionListOptions = {}) {
  const matches = sessions.filter(
    (item) =>
      Boolean(item.archived_at) === (options.archived ?? true) &&
      (!options.search || item.title.toLowerCase().includes(options.search.toLowerCase())) &&
      (!options.state ||
        (item.job.conversation_state
          ? item.job.conversation_state === options.state
          : options.state === 'idle'
            ? item.job.state === 'succeeded' || item.job.state === 'cancelled'
            : item.job.state === options.state)),
  );
  const facets = new Map<string | null, number>();
  for (const item of matches) facets.set(item.job.repo, (facets.get(item.job.repo) ?? 0) + 1);
  // The API sends the temporary workspaces' key as `temporary=true`.
  const filtered = matches.filter(
    (item) =>
      !options.repo ||
      (isTemporaryWorkspace(options.repo)
        ? item.job.repo === null
        : item.job.repo === options.repo),
  );
  const start = Number(options.cursor ?? '0');
  const end = start + (options.limit ?? 50);
  return {
    sessions: filtered.slice(start, end),
    total: options.includeSummary === false ? null : filtered.length,
    next_cursor: end < filtered.length ? String(end) : null,
    projects:
      options.includeSummary === false
        ? null
        : [...facets].map(([repo, count]) => ({ repo, count })),
  };
}

beforeEach(() => {
  intersections.clear();
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      private element: Element | null = null;
      constructor(private callback: IntersectionObserverCallback) {}
      observe(element: Element) {
        this.element = element;
        intersections.set(element, (isIntersecting) =>
          this.callback(
            [{ target: element, isIntersecting } as IntersectionObserverEntry],
            this as unknown as IntersectionObserver,
          ),
        );
      }
      disconnect() {
        if (this.element) intersections.delete(this.element);
      }
    },
  );
  mocks.locale = 'en';
  mocks.review.mockReturnValue(null);
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
  vi.unstubAllGlobals();
});

describe('archived session management', () => {
  it('offers four chat states and finds both successful and stopped conversations as idle', async () => {
    sessions = [
      session('published'),
      session('unpublished', {}, { published_pr_url: null }),
      session('stopped', {}, { state: 'cancelled' }),
      session('failed', {}, { state: 'failed' }),
      session('busy', { can_delete: false }, { state: 'cancelled', conversation_state: 'running' }),
    ];
    render(<ArchivedTasksView />);
    await screen.findByRole('link', { name: /Session published/ });
    expect(
      within(await screen.findByRole('link', { name: /Session busy/ })).getByText('Running'),
    ).toBeVisible();
    const status = screen.getByRole('combobox', { name: 'Filter by status' });
    expect(
      within(status)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['All statuses', 'Queued', 'Running', 'Idle', 'Failed']);
    fireEvent.change(status, { target: { value: 'idle' } });
    await waitFor(() =>
      expect(mocks.list).toHaveBeenCalledWith(
        expect.objectContaining({ state: 'idle' }),
        expect.any(AbortSignal),
      ),
    );
    for (const id of ['published', 'unpublished', 'stopped']) {
      const row = await screen.findByRole('link', { name: new RegExp(`Session ${id}`) });
      expect(within(row).getByText('Idle')).toBeInTheDocument();
    }
    expect(screen.queryByRole('link', { name: /Session failed/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Session busy/ })).not.toBeInTheDocument();
  });

  it('opens archived history without unarchiving and keeps running status visible', async () => {
    sessions = [session('running', { can_delete: false }, { state: 'running' })];
    render(<ArchivedTasksView />);
    const title = await screen.findByRole('link', { name: /Session running/ });
    expect(title).toHaveAttribute('href', '/job-running');
    expect(within(title).getByText('Running')).toBeVisible();
    expect(title.querySelector('time')).toHaveAttribute('datetime', '2026-09-09T16:27:00Z');
    expect(
      screen.getByRole('button', { name: 'Permanently delete Session running' }),
    ).toBeDisabled();
    expect(mocks.list).toHaveBeenCalledWith(
      expect.objectContaining({ repo: 'owner/alpha', includeSummary: false }),
      expect.any(AbortSignal),
    );
    expect(mocks.setArchived).not.toHaveBeenCalled();
  });

  it('marks an archived live session waiting for an answer beside its activity and archive date', async () => {
    sessions = [
      session('question', { can_delete: false }, { state: 'running', current_attempt_id: 7 }),
    ];
    mocks.review.mockReturnValue('question');
    render(<ArchivedTasksView />);
    const row = await screen.findByRole('link', { name: /Session question/ });
    const badge = within(row).getByRole('status', { name: 'Waiting for your answer' });
    expect(badge).toBeVisible();
    expect(badge).toHaveTextContent('Needs review');
    expect(badge).toHaveAttribute('title', 'Waiting for your answer');
    expect(within(row).getByText('Running')).toBeVisible();
    expect(row.querySelector('time')).toHaveAttribute('datetime', '2026-09-09T16:27:00Z');
    expect(mocks.review).toHaveBeenCalledWith({
      id: 'job-question',
      state: 'running',
      threadId: 'thread-question',
      currentAttemptId: 7,
      cancelRequested: undefined,
    });
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
    expect(screen.queryByRole('button', { name: /Show more/ })).not.toBeInTheDocument();
    await approachBoundary(alpha);
    expect(await within(alpha).findByRole('link', { name: /Session 9 / })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'beta' }));
    fireEvent.click(within(alpha).getByRole('button', { name: 'Unarchive Session 0' }));
    await waitFor(() => expect(within(alpha).getByText('54 sessions')).toBeVisible());
    expect(screen.getByRole('button', { name: 'beta' })).toHaveAttribute('aria-expanded', 'false');
    expect(await within(alpha).findByRole('link', { name: /Session 9 / })).toBeVisible();
    expect(await within(alpha).findByRole('link', { name: /Session 10 / })).toBeVisible();
    expect(within(alpha).getAllByRole('link')).toHaveLength(10);
  });

  it('reveals later projects on scroll without a more-projects button', async () => {
    sessions = Array.from({ length: 8 }, (_, index) =>
      session(`project-${index}`, {}, { repo: `owner/project-${index}` }),
    );
    render(<ArchivedTasksView />);
    await screen.findByRole('link', { name: /Session project-5/ });
    expect(screen.queryByRole('region', { name: 'owner/project-6' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Show more/ })).not.toBeInTheDocument();
    await approachBoundary(document.body);
    expect(await screen.findByRole('link', { name: /Session project-7/ })).toBeVisible();
  });

  it('stops automatic paging on failure and lets retry recover the same page', async () => {
    sessions = Array.from({ length: 6 }, (_, index) => session(String(index)));
    render(<ArchivedTasksView />);
    const alpha = await screen.findByRole('region', { name: 'owner/alpha' });
    await within(alpha).findByRole('link', { name: /Session 4 / });
    mocks.list.mockRejectedValueOnce(new Error('Network unavailable'));
    await approachBoundary(alpha);
    expect(await within(alpha).findByRole('alert')).toBeVisible();
    expect(intersections.size).toBe(0);
    fireEvent.click(within(alpha).getByRole('button', { name: 'Try again' }));
    expect(await within(alpha).findByRole('link', { name: /Session 5 / })).toBeVisible();
    expect(within(alpha).getAllByRole('link')).toHaveLength(6);
    expect(intersections.size).toBe(0);
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

  it('keeps filters editable while requests are pending and discards superseded results', async () => {
    sessions = [
      session('latest'),
      session('failed', { title: 'Deployment failure' }, { state: 'failed', repo: 'other/beta' }),
    ];
    render(<ArchivedTasksView />);
    await screen.findByRole('link', { name: /Session latest/ });
    const pending: {
      options: AgentSessionListOptions;
      signal: AbortSignal;
      resolve: (result: ReturnType<typeof listing>) => void;
    }[] = [];
    mocks.list.mockImplementation((options: AgentSessionListOptions, signal: AbortSignal) => {
      if (options.limit !== 1) return Promise.resolve(listing(options));
      return new Promise<ReturnType<typeof listing>>((resolve) =>
        pending.push({ options, signal, resolve }),
      );
    });
    const search = screen.getByRole('searchbox', { name: 'Search archived sessions' });
    const status = screen.getByRole('combobox', { name: 'Filter by status' });
    const project = screen.getByRole('combobox', { name: 'Filter by project' });
    search.focus();
    fireEvent.change(search, { target: { value: 'Deployment' } });
    await waitFor(() => expect(pending).toHaveLength(1));

    expect(search).toBeEnabled();
    expect(search).toHaveFocus();
    expect(status).toBeEnabled();
    expect(project).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Permanently delete filtered' })).toBeDisabled();

    fireEvent.change(search, { target: { value: 'failure' } });
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(search).toHaveValue('failure');
    expect(search).toHaveFocus();
    expect(pending[0].signal.aborted).toBe(true);

    fireEvent.change(status, { target: { value: 'failed' } });
    await waitFor(() => expect(pending).toHaveLength(3));
    expect(pending[1].signal.aborted).toBe(true);
    expect(project).toBeEnabled();
    fireEvent.change(project, { target: { value: 'other/beta' } });
    await waitFor(() => expect(pending).toHaveLength(4));
    expect(pending[2].signal.aborted).toBe(true);
    expect(pending[3].options).toMatchObject({
      search: 'failure',
      state: 'failed',
      repo: 'other/beta',
    });

    await act(async () => pending[3].resolve(listing(pending[3].options)));
    expect(await screen.findByRole('link', { name: /Deployment failure/ })).toBeVisible();
    await act(async () => pending[0].resolve(listing()));

    expect(screen.getByRole('link', { name: /Deployment failure/ })).toBeVisible();
    expect(screen.queryByRole('link', { name: /Session latest/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Permanently delete filtered' })).toBeEnabled();
    expect(mocks.deleteSessions).not.toHaveBeenCalled();
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
      expect(screen.getByRole('button', { name: 'Permanently delete filtered' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Permanently delete filtered' }));
    const dialog = await screen.findByRole('dialog', { name: 'Permanently delete 100 sessions?' });
    expect(within(dialog).getByText('Scope: current search and filters')).toBeVisible();
    expect(
      within(dialog).getByText(/2 sessions preparing a fork or publishing are excluded/),
    ).toBeVisible();
    expect(mocks.list).toHaveBeenCalledWith(
      expect.objectContaining({ repo: 'owner/alpha', limit: 100, cursor: '100' }),
      expect.any(AbortSignal),
    );
    expect(mocks.deleteSessions).not.toHaveBeenCalled();
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Permanently delete 100 sessions' }),
    );
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
    fireEvent.click(screen.getByRole('menuitem', { name: 'Permanently delete group (1)' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Scope: archived sessions in owner/alpha')).toBeVisible();
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mocks.deleteSessions).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Actions for owner/alpha' })).toHaveFocus();
  });

  it.each(['delete', 'unarchive'])(
    'rejects an oversized %s scope after one bulk page',
    async (action) => {
      sessions = Array.from({ length: 50_000 }, (_, index) => session(String(index)));
      render(<ArchivedTasksView />);
      await screen.findByRole('link', { name: /Session 0 / });
      if (action === 'delete') {
        fireEvent.click(screen.getByRole('button', { name: 'Permanently delete all' }));
      } else {
        fireEvent.click(screen.getByRole('button', { name: 'Actions for owner/alpha' }));
        fireEvent.click(screen.getByRole('menuitem', { name: /Unarchive group/ }));
      }
      expect(await screen.findByRole('alert')).toHaveTextContent('Select at most 1000 sessions');
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      const bulkCalls = mocks.list.mock.calls.filter(([options]) => options.limit === 100);
      expect(bulkCalls).toHaveLength(1);
      expect(bulkCalls[0][0]).toMatchObject({ includeSummary: true });
      expect(bulkCalls[0][0].cursor).toBeUndefined();
      expect(mocks.deleteSessions).not.toHaveBeenCalled();
      expect(mocks.setArchived).not.toHaveBeenCalled();
    },
  );

  it('bounds collection if the archive grows beyond the first page total', async () => {
    sessions = Array.from({ length: 1000 }, (_, index) => session(String(index)));
    mocks.list.mockImplementation(async (options: AgentSessionListOptions) => {
      const page = listing(options);
      if (options.limit === 100 && !options.cursor) sessions.push(session('new'));
      return page;
    });
    render(<ArchivedTasksView />);
    await screen.findByRole('link', { name: /Session 0 / });
    fireEvent.click(screen.getByRole('button', { name: 'Permanently delete all' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Select at most 1000 sessions');
    const bulkCalls = mocks.list.mock.calls.filter(([options]) => options.limit === 100);
    expect(bulkCalls).toHaveLength(11);
    expect(bulkCalls.slice(1).every(([options]) => options.includeSummary === false)).toBe(true);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mocks.deleteSessions).not.toHaveBeenCalled();
  });

  it.each([
    ['fork_in_progress', 'being forked'],
    ['publication_pending', 'unfinished publication'],
    ['not_found', 'no longer available'],
  ])(
    'discards a stale delete selection on %s and retries with refreshed IDs',
    async (code, message) => {
      sessions = [session('one'), session('two')];
      render(<ArchivedTasksView />);
      await screen.findByRole('link', { name: /Session one/ });
      fireEvent.click(screen.getByRole('button', { name: 'Permanently delete all' }));
      const dialog = await screen.findByRole('dialog');
      if (code === 'not_found') sessions = [session('two')];
      else sessions[0] = session('one', { can_delete: false });
      mocks.deleteSessions.mockRejectedValueOnce(
        new APIError(code, 'Server conflict', code === 'not_found' ? 404 : 409),
      );
      fireEvent.click(
        within(dialog).getByRole('button', { name: 'Permanently delete 2 sessions' }),
      );
      expect(await screen.findByRole('alert')).toHaveTextContent(message);
      expect(screen.getByRole('alert')).toHaveTextContent('Review the refreshed list');
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Permanently delete all' })).toBeEnabled(),
      );
      fireEvent.click(screen.getByRole('button', { name: 'Permanently delete all' }));
      const refreshed = await screen.findByRole('dialog');
      fireEvent.click(
        within(refreshed).getByRole('button', { name: 'Permanently delete 1 session' }),
      );
      await waitFor(() => expect(mocks.deleteSessions).toHaveBeenLastCalledWith(['thread-two']));
    },
  );

  it.each(['unarchive', 'undo'])(
    'refreshes a disappeared session after failed %s',
    async (action) => {
      render(<ArchivedTasksView />);
      await screen.findByRole('button', { name: 'Unarchive Session one' });
      if (action === 'undo') {
        fireEvent.click(screen.getByRole('button', { name: 'Unarchive Session one' }));
        await waitFor(() => expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled());
      }
      sessions = [];
      mocks.setArchived.mockRejectedValueOnce(new APIError('not_found', 'Unavailable', 404));
      fireEvent.click(
        screen.getByRole('button', { name: action === 'undo' ? 'Undo' : 'Unarchive Session one' }),
      );
      expect(await screen.findByRole('alert')).toHaveTextContent('no longer available');
      expect(await screen.findByText('All caught up')).toBeVisible();
      expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument();
    },
  );

  it('shows a retryable list failure and retains rows when deletion fails', async () => {
    mocks.list.mockRejectedValueOnce(new Error('connection unavailable'));
    render(<ArchivedTasksView />);
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Permanently delete Session one' }));
    const dialog = await screen.findByRole('dialog');
    mocks.deleteSessions.mockRejectedValueOnce(new Error('connection unavailable'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Permanently delete 1 session' }));
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
    expect(
      screen.getByRole('button', { name: 'Permanently delete Session running' }),
    ).toBeDisabled();
    sessions = [session('running')];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(
      screen.getByRole('button', { name: 'Permanently delete Session running' }),
    ).toBeEnabled();
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
    expect(screen.getByRole('button', { name: 'Permanently delete filtered' })).toBeDisabled();
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
