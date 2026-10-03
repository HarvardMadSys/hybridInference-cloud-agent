// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AdminConversationDetail,
  AdminConversationMatch,
  AdminConversationsPage,
} from '@/lib/api/adminConversations';
import { getAdminConversation, getAdminConversations } from '@/lib/api/adminConversations';
import { getAdminRuns } from '@/lib/api/adminRuns';
import { APIError } from '@/lib/utils/errors';

const context = vi.hoisted(() => ({ admin: true, user: 'admin', query: '', threadId: 'thread-1' }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      user: {
        id: context.user,
        agent_access: {
          allowed: true,
          permissions: context.admin ? ['agent.use', 'agent.admin'] : ['agent.use'],
        },
      },
    },
  }),
}));
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(context.query),
  useParams: () => ({ threadId: context.threadId }),
}));
vi.mock('@/lib/api/adminConversations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminConversations')>()),
  getAdminConversations: vi.fn(),
  getAdminConversation: vi.fn(),
}));
vi.mock('@/lib/api/adminRuns', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminRuns')>()),
  getAdminRuns: vi.fn(),
}));

import List from './page';
import Detail from './[threadId]/page';
import PreviousEntry from '../runs/page';
import ActivityList from '../activity/page';
import SessionList from '../sessions/page';
import SessionDetail from '../sessions/[threadId]/page';

const since = '2026-09-10T08:00:00Z';
const until = '2026-09-17T08:00:00Z';
function conversation(overrides: Partial<AdminConversationMatch> = {}): AdminConversationMatch {
  return {
    thread_id: 'thread-1',
    title: 'Fix login',
    user_id: 'owner',
    repo: 'org/repo',
    created_at: since,
    last_activity_at: until,
    archived_at: null,
    deleted_at: null,
    state: 'idle',
    turn_count: 3,
    latest_job_id: 'job-3',
    latest_turn_no: 3,
    latest_turn_state: 'succeeded',
    latest_runtime: 'claude-code',
    latest_model: 'model-b',
    last_matching_turn_at: since,
    ...overrides,
  };
}
function page(rows = [conversation()], next: string | null = null): AdminConversationsPage {
  return { conversations: rows, since, until, next_cursor: next };
}
function detail(): AdminConversationDetail {
  return {
    conversation: conversation(),
    next_cursor: null,
    turns: [3, 2, 1].map((number) => ({
      id: `job-${number}`,
      thread_id: 'thread-1',
      title: 'Fix login',
      turn_no: number,
      user_id: 'owner',
      repo: 'org/repo',
      runtime: 'claude-code',
      model: number === 3 ? 'model-b' : 'model-a',
      reasoning_effort: null,
      execution_profile: 'trusted-v1',
      state: 'succeeded',
      created_at: since,
      updated_at: until,
      hosts: [],
      tool_calls: number,
      matches_filters: number !== 3,
      fork_source_job_id: null,
    })),
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(context, { admin: true, user: 'admin', query: '', threadId: 'thread-1' });
  vi.mocked(getAdminConversations).mockResolvedValue(page());
  vi.mocked(getAdminConversation).mockResolvedValue(detail());
  vi.mocked(getAdminRuns).mockResolvedValue({ runs: [], next_cursor: null, since, until });
});
afterEach(cleanup);

describe('session navigation', () => {
  it('makes a titled session the row identity and separates state from turn results', async () => {
    render(<List />);
    const link = await screen.findByRole('link', { name: 'Fix login' });
    const target = new URL(link.getAttribute('href')!, 'http://test');
    expect(target.pathname).toBe('/admin/sessions/thread-1');
    expect(target.searchParams.get('since')).toBe(since);
    // The session ID is the title's tooltip rather than a line of its own.
    expect(link).toHaveAttribute('title', 'thread-1');
    expect(screen.queryByText('job-3')).not.toBeInTheDocument();
    expect(screen.getByText('idle')).toBeInTheDocument();
    expect(screen.queryByText('succeeded', { selector: 'span' })).not.toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '3' })).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'CPU' })).not.toBeInTheDocument();
    // A turn is reached through its session; there is no second run list.
    expect(screen.queryByRole('link', { name: /execution records/i })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Activity' })).toHaveAttribute('aria-current', 'page');
    const filters = vi.mocked(getAdminConversations).mock.calls[0][0];
    expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(7 * 86400000);
  });

  it.each([
    ['/admin/activity', ActivityList],
    ['/admin/sessions', SessionList],
    ['/admin/runs', PreviousEntry],
  ])('opens the session list at %s', async (_, Entry) => {
    render(<Entry />);
    expect(await screen.findByRole('heading', { name: 'Sessions' })).toBeInTheDocument();
    await screen.findByRole('link', { name: 'Fix login' });
  });

  it('falls back to the session ID and keeps sessions with identical titles separate', async () => {
    vi.mocked(getAdminConversations).mockResolvedValue(
      page([
        conversation(),
        conversation({ thread_id: 'thread-2' }),
        conversation({ thread_id: 'thread-3', title: null }),
      ]),
    );
    render(<List />);
    expect(await screen.findAllByRole('link', { name: 'Fix login' })).toHaveLength(2);
    expect(screen.getByRole('link', { name: 'thread-3' }).getAttribute('href')).toContain(
      '/admin/sessions/thread-3?',
    );
  });

  it('preserves usage filters, a fixed window, and pagination in session links', async () => {
    context.query = new URLSearchParams({
      since,
      until,
      user_id: 'owner',
      repo: 'org/repo',
      model: 'model-a',
      runtime: 'claude-code',
    }).toString();
    vi.mocked(getAdminConversations)
      .mockResolvedValueOnce(page([conversation()], 'next'))
      .mockResolvedValue(page([conversation({ thread_id: 'thread-2', title: 'Second chat' })]));
    render(<List />);
    const link = await screen.findByRole('link', { name: 'Fix login' });
    // A linked window is no preset, so the toolbar names its days.
    expect(screen.getByLabelText('Turn created')).toHaveValue('custom');
    expect(screen.getByText('Sep 10 – Sep 17')).toBeInTheDocument();
    // No control shows the identities the link arrived with, so they are stated.
    expect(
      screen.getByText('User: owner · Repository: org/repo · Agent: claude-code · Model: model-a'),
    ).toBeInTheDocument();
    const params = new URL(link.getAttribute('href')!, 'http://test').searchParams;
    expect(Object.fromEntries(params)).toEqual(
      Object.fromEntries(new URLSearchParams(context.query)),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Load more sessions' }));
    await screen.findByRole('link', { name: 'Second chat' });
    expect(getAdminConversations).toHaveBeenLastCalledWith(
      expect.objectContaining({ since, until, cursor: 'next', model: 'model-a' }),
      expect.any(AbortSignal),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(getAdminConversations).toHaveBeenCalledTimes(3));
    expect(vi.mocked(getAdminConversations).mock.lastCall![0]).toMatchObject({
      since,
      until,
      model: 'model-a',
    });
    expect(vi.mocked(getAdminConversations).mock.lastCall![0].cursor).toBeUndefined();
  });

  it('keeps a linked one-day window when changing turn result and discards stale pages', async () => {
    const dayEnd = '2026-09-11T08:00:00Z';
    context.query = new URLSearchParams({ since, until: dayEnd, model: 'model-a' }).toString();
    const pending = deferred<AdminConversationsPage>();
    vi.mocked(getAdminConversations)
      .mockResolvedValueOnce(page([conversation()], 'next'))
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(page([conversation({ title: 'Failed conversation' })]));
    render(<List />);
    await screen.findByRole('link', { name: 'Fix login' });
    fireEvent.click(screen.getByRole('button', { name: 'Load more sessions' }));
    fireEvent.change(screen.getByLabelText('Turn result'), { target: { value: 'failed' } });
    await screen.findByRole('link', { name: 'Failed conversation' });
    expect(vi.mocked(getAdminConversations).mock.lastCall![0]).toMatchObject({
      since,
      until: dayEnd,
      model: 'model-a',
      state: 'failed',
    });
    expect(vi.mocked(getAdminConversations).mock.calls[1][1]?.aborted).toBe(true);
    await act(async () => pending.resolve(page([conversation({ title: 'Stale' })])));
    expect(screen.queryByText('Stale')).not.toBeInTheDocument();
  });

  it.each(['since=bad&until=also-bad', 'since=2026-09-01', 'unsupported=yes'])(
    'rejects broken filter links: %s',
    async (query) => {
      context.query = query;
      render(<List />);
      expect(screen.getByRole('alert')).toBeInTheDocument();
      expect(getAdminConversations).not.toHaveBeenCalled();
    },
  );

  it('shows empty results and recovers a failed initial request on retry', async () => {
    vi.mocked(getAdminConversations)
      .mockRejectedValueOnce(new Error('Try again'))
      .mockResolvedValueOnce(page([]));
    render(<List />);
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('No sessions match these filters.')).toBeInTheDocument();
  });

  it('clears cross-user rows on a denied later page', async () => {
    vi.mocked(getAdminConversations)
      .mockResolvedValueOnce(page([conversation()], 'next'))
      .mockRejectedValueOnce(new APIError('FORBIDDEN', 'Denied', 403));
    render(<List />);
    await screen.findByRole('link', { name: 'Fix login' });
    fireEvent.click(screen.getByRole('button', { name: 'Load more sessions' }));
    await screen.findByText('Admin access required');
    expect(screen.queryByText('Fix login')).not.toBeInTheDocument();
  });

  it('reloads a new query and aborts the old request', async () => {
    const pending = deferred<AdminConversationsPage>();
    vi.mocked(getAdminConversations).mockReturnValueOnce(pending.promise);
    const view = render(<List />);
    context.query = new URLSearchParams({ since, until, model: 'new-model' }).toString();
    view.rerender(<List />);
    await screen.findByRole('link', { name: 'Fix login' });
    expect(vi.mocked(getAdminConversations).mock.calls[0][1]?.aborted).toBe(true);
    expect(vi.mocked(getAdminConversations).mock.lastCall![0].model).toBe('new-model');
    await act(async () => pending.resolve(page([conversation({ title: 'Stale' })])));
    expect(screen.queryByText('Stale')).not.toBeInTheDocument();
  });
});

describe('level switch', () => {
  it('switches between sessions and turns without losing the selection', async () => {
    render(<List />);
    await screen.findByRole('link', { name: 'Fix login' });
    expect(screen.getByRole('button', { name: 'Sessions', pressed: true })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Turn result'), { target: { value: 'failed' } });
    fireEvent.change(screen.getByLabelText('Turn created'), { target: { value: '30' } });
    await waitFor(() => expect(getAdminConversations).toHaveBeenCalledTimes(3));
    const selection = vi.mocked(getAdminConversations).mock.lastCall![0];
    expect(getAdminRuns).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Turns' }));
    expect(screen.getByRole('button', { name: 'Turns', pressed: true })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Level' })).toHaveAttribute(
      'title',
      'Turns created in the selected window from every session, newest first.',
    );
    await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(1));
    // The same turns, read one per row: the window and the result carry over.
    expect(vi.mocked(getAdminRuns).mock.lastCall![0]).toEqual(selection);
    expect(screen.getByLabelText('Turn result')).toHaveValue('failed');
    expect(screen.getByLabelText('Turn created')).toHaveValue('30');

    fireEvent.click(screen.getByRole('button', { name: 'Sessions' }));
    await waitFor(() => expect(getAdminConversations).toHaveBeenCalledTimes(4));
    expect(vi.mocked(getAdminConversations).mock.lastCall![0]).toEqual(selection);
  });

  it('opens the turn level from a link and keeps its filters', async () => {
    context.query = new URLSearchParams({
      level: 'turns',
      since,
      until,
      model: 'model-a',
    }).toString();
    render(<List />);
    await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: 'Turns', pressed: true })).toBeInTheDocument();
    expect(vi.mocked(getAdminRuns).mock.lastCall![0]).toMatchObject({
      since,
      until,
      model: 'model-a',
    });
    expect(getAdminConversations).not.toHaveBeenCalled();
  });
});

describe('session turns', () => {
  it('keeps all turns in order, marks matches and carries selection to execution details', async () => {
    context.query = new URLSearchParams({ since, until, model: 'model-a' }).toString();
    render(<Detail />);
    await screen.findByRole('heading', { name: 'Fix login' });
    expect(screen.getAllByRole('link', { name: /^Turn / }).map((link) => link.textContent)).toEqual(
      ['Turn 3', 'Turn 2', 'Turn 1'],
    );
    expect(screen.getAllByText('Matches filters')).toHaveLength(2);
    expect(screen.getByText('Outside filters')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Turn 3' })).toHaveAttribute(
      'href',
      `/admin/runs/job-3?${context.query}`,
    );
    expect(screen.getByRole('link', { name: '← All sessions' })).toHaveAttribute(
      'href',
      `/admin/activity?${context.query}`,
    );
  });

  it('opens a session at its /admin/sessions address', async () => {
    render(<SessionDetail />);
    await screen.findByRole('heading', { name: 'Fix login' });
    expect(screen.getByRole('link', { name: '← All sessions' })).toHaveAttribute(
      'href',
      '/admin/activity',
    );
  });

  it('loads the entire history without applying the list default window', async () => {
    render(<Detail />);
    await screen.findByRole('link', { name: 'Turn 1' });
    expect(getAdminConversation).toHaveBeenCalledWith('thread-1', {}, expect.any(AbortSignal));
    expect(screen.queryByText('Matches filters')).not.toBeInTheDocument();
  });

  it('loads older turns without replacing newer ones and identifies inherited executions', async () => {
    const all = detail();
    const inherited = { ...all.turns[2], fork_source_job_id: 'original-job' };
    vi.mocked(getAdminConversation)
      .mockResolvedValueOnce({ ...all, turns: [all.turns[0]], next_cursor: 'older' })
      .mockResolvedValueOnce({ ...all, turns: [all.turns[1], inherited] });
    render(<Detail />);
    await screen.findByRole('link', { name: 'Turn 3' });
    fireEvent.click(screen.getByRole('button', { name: 'Load older turns' }));
    await screen.findByRole('link', { name: 'Turn 1' });
    expect(screen.getAllByRole('link', { name: /^Turn / })).toHaveLength(3);
    expect(screen.getByRole('link', { name: /Inherited/ })).toHaveAttribute(
      'href',
      '/admin/runs/original-job',
    );
    expect(getAdminConversation).toHaveBeenLastCalledWith(
      'thread-1',
      { cursor: 'older' },
      expect.any(AbortSignal),
    );
  });

  it('clears the previous session and aborts older-turn reads when navigating', async () => {
    const pending = deferred<AdminConversationDetail>();
    vi.mocked(getAdminConversation)
      .mockResolvedValueOnce({ ...detail(), next_cursor: 'older' })
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce({
        ...detail(),
        conversation: conversation({ thread_id: 'thread-2', title: 'New chat' }),
        turns: [],
      });
    const view = render(<Detail />);
    await screen.findByRole('link', { name: 'Turn 3' });
    fireEvent.click(screen.getByRole('button', { name: 'Load older turns' }));
    context.threadId = 'thread-2';
    view.rerender(<Detail />);
    await screen.findByRole('heading', { name: 'New chat' });
    expect(vi.mocked(getAdminConversation).mock.calls[1][2]?.aborted).toBe(true);
    await act(async () => pending.resolve(detail()));
    expect(screen.queryByRole('link', { name: 'Turn 3' })).not.toBeInTheDocument();
  });

  it('clears a session after admin access is revoked during paging', async () => {
    vi.mocked(getAdminConversation)
      .mockResolvedValueOnce({ ...detail(), next_cursor: 'older' })
      .mockRejectedValueOnce(new APIError('FORBIDDEN', 'Denied', 403));
    render(<Detail />);
    await screen.findByRole('link', { name: 'Turn 3' });
    fireEvent.click(screen.getByRole('button', { name: 'Load older turns' }));
    await screen.findByText('Admin access required');
    expect(screen.queryByText('Fix login')).not.toBeInTheDocument();
  });

  it.each([List, Detail])('requires admin access before reading any data', (Component) => {
    context.admin = false;
    render(<Component />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(getAdminConversation).not.toHaveBeenCalled();
    expect(getAdminConversations).not.toHaveBeenCalled();
  });
});
