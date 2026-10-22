// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { useMemo } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
  listAgentConversations,
  type AgentConversationApi,
  type AgentJobApi,
} from '@/lib/api/agents';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { createProjectRefreshTracker } from './sidebarProjectRefresh';
import { createSidebarReadQueue, type ConversationReader } from './sidebarReadQueue';
import { useConversationList } from './useConversationList';

vi.mock('@/lib/api/agents', () => ({ listAgentConversations: vi.fn() }));
afterEach(cleanup);
beforeEach(() => vi.resetAllMocks());
const project = (repo: string, token: string | null = 'initial') => ({
  repo,
  task_count: 1,
  active_count: 0,
  last_activity_at: null,
  refresh_token: token,
  membership_token: `members:${repo}`,
});
const projects = Array.from({ length: 20 }, (_, n) => project(`owner/repo-${n}`));
function row(repo: string, n: number): AgentConversationApi {
  return {
    job: {
      id: `${repo}-${n}`,
      thread_id: `${repo}-${n}`,
      repo,
      task_prompt: 'task',
      runtime: 'codex',
      model: 'model',
      state: 'succeeded',
    } as AgentJobApi,
    title: `${repo}-${n}`,
    archived: false,
    last_activity_at: `2026-10-${String(20 - n).padStart(2, '0')}T00:00:00Z`,
  };
}
function Folder({
  repo,
  signals,
  read,
}: {
  repo: string;
  signals: ReturnType<ReturnType<typeof createProjectRefreshTracker>['forRepo']>;
  read: ConversationReader;
}) {
  const list = useConversationList({
    repo,
    ...signals,
    reorder: signals.activityMoved,
    readPage: read,
  });
  return (
    <div data-testid={repo}>
      {list.rows.length}
      {list.error && <span>{list.error}</span>}
      <button onClick={list.loadMore}>More {repo}</button>
    </div>
  );
}
function Tree({
  data = projects,
  refresh = 0,
  reorder = 0,
  owner = 'alice',
}: {
  data?: typeof projects;
  refresh?: number;
  reorder?: number;
  owner?: string;
}) {
  const scope = useMemo(
    () => ({ owner, tracker: createProjectRefreshTracker(), read: createSidebarReadQueue() }),
    [owner],
  );
  scope.tracker.observe(data, refresh, reorder);
  return (
    <>
      {data.map((p) => (
        <Folder
          key={p.repo}
          repo={p.repo}
          signals={scope.tracker.forRepo(p.repo)}
          read={scope.read}
        />
      ))}
    </>
  );
}
async function ready() {
  await waitFor(() => expect(screen.getByTestId('owner/repo-19').textContent).toMatch(/^1More/));
}

it('refreshes one of 20 expanded folders for one changed token, and none on unchanged fallback or stream-gap snapshots', async () => {
  vi.mocked(listAgentConversations).mockImplementation(async (query) => ({
    conversations: [row(query!.repo!, 0)],
    next_cursor: null,
  }));
  const view = render(<Tree />);
  await ready();
  expect(listAgentConversations).toHaveBeenCalledTimes(20);
  vi.mocked(listAgentConversations).mockClear();
  view.rerender(<Tree data={projects.map((p) => ({ ...p }))} refresh={1} />);
  await act(async () => {});
  expect(listAgentConversations).not.toHaveBeenCalled();
  const changed = projects.map((p, i) => (i === 7 ? { ...p, refresh_token: 'changed' } : p));
  view.rerender(<Tree data={changed} refresh={2} />);
  await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(1));
  expect(listAgentConversations).toHaveBeenCalledWith(
    expect.objectContaining({ repo: 'owner/repo-7' }),
    expect.any(AbortSignal),
  );
  vi.mocked(listAgentConversations).mockClear();
  // A stream gap rereads all tokens, so missed mutations need no pushed repo mapping.
  view.rerender(
    <Tree
      data={changed.map((p, i) => (i === 12 ? { ...p, refresh_token: 'gap-change' } : p))}
      refresh={3}
    />,
  );
  await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(1));
  expect(listAgentConversations).toHaveBeenCalledWith(
    expect.objectContaining({ repo: 'owner/repo-12' }),
    expect.any(AbortSignal),
  );
});

it('patches local mutations without waking the other 19 managed folder readers', async () => {
  vi.mocked(listAgentConversations).mockImplementation(async (query) => ({
    conversations: [row(query!.repo!, 0)],
    next_cursor: null,
  }));
  const view = render(<Tree />);
  await ready();
  vi.mocked(listAgentConversations).mockClear();
  act(() => announceAgentSessionsChanged({ threadIds: ['owner/repo-3-0'], title: 'renamed' }));
  expect(listAgentConversations).not.toHaveBeenCalled();
  view.rerender(
    <Tree
      data={projects.map((p, i) => (i === 3 ? { ...p, refresh_token: 'renamed' } : p))}
      refresh={1}
    />,
  );
  await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(1));
});

it('never loses a full invalidation when a fresh grouped snapshot arrives without another global tick', () => {
  const tracker = createProjectRefreshTracker();
  tracker.observe(projects, 0, 0);
  expect(tracker.forRepo('owner/repo-0').refreshKey).toBe('initial');
  tracker.observe(
    projects.map((p, i) => (i === 0 ? { ...p, refresh_token: 'new' } : p)),
    0,
    0,
  );
  expect(tracker.forRepo('owner/repo-0').refreshKey).toBe('new');
  expect(tracker.forRepo('owner/repo-1').refreshKey).toBe('initial');
});

it('retries only a failed folder on the next unchanged grouped snapshot', async () => {
  vi.mocked(listAgentConversations).mockImplementation(async (query) => ({
    conversations: [row(query!.repo!, 0)],
    next_cursor: null,
  }));
  const view = render(<Tree />);
  await ready();
  vi.mocked(listAgentConversations)
    .mockClear()
    .mockRejectedValueOnce(new Error('temporary failure'));
  const changed = projects.map((p, i) => (i === 7 ? { ...p, refresh_token: 'changed' } : p));
  view.rerender(<Tree data={changed} refresh={1} />);
  await screen.findByText('temporary failure');
  expect(listAgentConversations).toHaveBeenCalledTimes(1);
  view.rerender(<Tree data={changed.map((p) => ({ ...p }))} refresh={2} />);
  await waitFor(() => expect(screen.queryByText('temporary failure')).toBeNull());
  expect(listAgentConversations).toHaveBeenCalledTimes(2);
  expect(
    vi.mocked(listAgentConversations).mock.calls.every(([query]) => query?.repo === 'owner/repo-7'),
  ).toBe(true);
});

it.each(['full', 'membership'])(
  'preserves loaded depth and cursors for a changed folder (%s)',
  async (kind) => {
    vi.mocked(listAgentConversations).mockImplementation(async (query) => {
      if (query?.repo !== 'owner/repo-7')
        return { conversations: [row(query!.repo!, 0)], next_cursor: null };
      const start = Number(query.cursor ?? 0);
      const count = Math.min(query.limit ?? 50, 250 - start);
      return {
        conversations: Array.from({ length: count }, (_, i) => row(query.repo!, start + i)),
        next_cursor: start + count < 250 ? String(start + count) : null,
      };
    });
    const view = render(<Tree />);
    await ready();
    for (const count of [100, 150, 200, 250]) {
      act(() => screen.getByText('More owner/repo-7').click());
      await waitFor(() =>
        expect(screen.getByTestId('owner/repo-7').textContent).toMatch(new RegExp(`^${count}More`)),
      );
    }
    vi.mocked(listAgentConversations).mockClear();
    const changed = projects.map((p, i) => {
      if (i !== 7) return p;
      return {
        ...p,
        refresh_token: 'changed',
        membership_token: kind === 'membership' ? 'swapped' : p.membership_token,
      };
    });
    view.rerender(
      <Tree
        data={changed}
        refresh={kind === 'full' ? 1 : 0}
        reorder={kind === 'membership' ? 1 : 0}
      />,
    );
    await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(
      vi.mocked(listAgentConversations).mock.calls.map(([q]) => [q?.repo, q?.cursor, q?.limit]),
    ).toEqual([
      ['owner/repo-7', undefined, 200],
      ['owner/repo-7', '200', 50],
    ]);
    expect(screen.getByTestId('owner/repo-7').textContent).toMatch(/^250More/);
  },
);

it('keeps activity-only refreshes to the changed folder first page', async () => {
  vi.mocked(listAgentConversations).mockImplementation(async (query) => ({
    conversations: [row(query!.repo!, 0)],
    next_cursor: null,
  }));
  const view = render(<Tree />);
  await ready();
  vi.mocked(listAgentConversations).mockClear();
  view.rerender(
    <Tree
      data={projects.map((p, i) => (i === 3 ? { ...p, refresh_token: 'activity' } : p))}
      reorder={1}
    />,
  );
  await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(1));
  expect(listAgentConversations).toHaveBeenCalledWith(
    expect.objectContaining({ repo: 'owner/repo-3', limit: 50 }),
    expect.any(AbortSignal),
  );
});

it('retains correct legacy refreshes when grouped tokens are absent', async () => {
  vi.mocked(listAgentConversations).mockImplementation(async (query) => ({
    conversations: [row(query!.repo!, 0)],
    next_cursor: null,
  }));
  const legacy = projects.map((p) => ({ ...p, refresh_token: null }));
  const view = render(<Tree data={legacy} />);
  await ready();
  vi.mocked(listAgentConversations).mockClear();
  view.rerender(<Tree data={legacy.map((p) => ({ ...p }))} refresh={1} />);
  await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(20));
});

it('does not acknowledge changed activity while an older folder snapshot is in flight', async () => {
  let finish!: (value: Awaited<ReturnType<typeof listAgentConversations>>) => void;
  vi.mocked(listAgentConversations)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValue({ conversations: [row('owner/repo-0', 0)], next_cursor: null });
  const view = render(<Tree data={[projects[0]]} />);
  await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(1));
  view.rerender(<Tree data={[{ ...projects[0], refresh_token: 'activity' }]} reorder={1} />);
  await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByTestId('owner/repo-0').textContent).toMatch(/^1More/));
  await act(async () => finish({ conversations: [], next_cursor: null }));
  expect(screen.getByTestId('owner/repo-0').textContent).toMatch(/^1More/);
});
