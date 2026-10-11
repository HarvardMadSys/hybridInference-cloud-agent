// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentConversationApi, AgentJobApi, AgentSessionListOptions } from '@/lib/api/agents';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
const api = vi.hoisted(() => ({
  list: vi.fn(),
  projects: vi.fn(),
  config: vi.fn(),
  get: vi.fn(),
  thread: vi.fn(),
  archive: vi.fn(),
  restore: vi.fn(),
  sessions: vi.fn(),
  setArchived: vi.fn(),
  pin: vi.fn(),
  events: vi.fn(),
  git: vi.fn(),
  stream: vi.fn(),
  replace: vi.fn(),
  pathname: '/',
  params: '',
  user: 'alice',
}));
vi.mock('next/navigation', () => ({
  usePathname: () => api.pathname,
  useSearchParams: () => new URLSearchParams(api.params),
  useRouter: () => ({ replace: api.replace }),
}));
vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  const t = (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
    translate('en', key, vars);
  return {
    useAuth: () => ({ state: { user: { id: api.user } } }),
    useT: () => t,
    useLocale: () => ({ locale: 'en', t }),
  };
});
vi.mock('@/components/features/settings/SettingsLauncher', () => ({
  SettingsLauncher: () => null,
}));
vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  listAgentConversations: api.list,
  listAgentProjects: api.projects,
  getAgentConfig: api.config,
  getAgentJob: api.get,
  getAgentJobThread: api.thread,
  listAllAgentThreadEvents: api.events,
  getAgentJobGit: api.git,
  streamAgentJob: api.stream,
  archiveAgentJob: api.archive,
  restoreAgentJob: api.restore,
  listAgentSessions: api.sessions,
  setAgentSessionsArchived: api.setArchived,
  pinAgentJob: api.pin,
  unpinAgentJob: api.pin,
}));
import { AgentsSidebar } from './AgentsSidebar';
import { SkillDialog } from './skills/SkillDialog';
import { AgentReadProvider } from './AgentReadProvider';
import { useAgentJob } from './useAgentJobs';
import { ArchivedTasksView } from './ArchivedTasksView';
import { announceConversationRestored } from './conversationEvents';
import { DEFAULT_PREFERENCES, sidebarPreferencesKey } from './sidebarPreferences';
import {
  PUSH_BATCH_MS,
  receiveHint,
  receiveJobChange,
  receiveSync,
  receiveThreadChange,
  resetAgentPush,
} from './agentPush';
function conversation(id: string, repo = 'owner/repo'): AgentConversationApi {
  return {
    job: {
      id,
      thread_id: `thread-${id}`,
      turn_no: 1,
      repo,
      state: 'succeeded',
      task_prompt: 'Follow-up',
      created_at: '2026-09-01T00:00:00Z',
    } as AgentJobApi,
    title: `Task ${id}`,
    archived: false,
    last_activity_at: '2026-09-01T00:00:00Z',
  };
}
const base = [conversation('a'), conversation('b')];
function DetailRequestReader() {
  const { job } = useAgentJob(api.pathname.slice(1));
  return (
    <p data-testid="detail-request-reader">
      {job?.id ?? 'loading'}:{job?.title}
    </p>
  );
}
beforeEach(() => {
  resetAgentPush();
  localStorage.clear();
  api.pathname = '/';
  api.params = '';
  api.user = 'alice';
  api.list.mockResolvedValue({ conversations: base, next_cursor: null });
  api.projects.mockResolvedValue([
    {
      repo: 'owner/repo',
      task_count: 2,
      active_count: 0,
      last_activity_at: '2026-09-01T00:00:00Z',
    },
  ]);
  api.get.mockRejectedValue(new Error('not a task'));
  api.thread.mockResolvedValue(null);
  api.events.mockResolvedValue([]);
  api.git.mockResolvedValue({ available: false });
  api.archive.mockResolvedValue({});
});
afterEach(() => {
  cleanup();
  resetAgentPush();
  vi.useRealTimers();
  vi.resetAllMocks();
});
describe('sidebar navigation interactions', () => {
  it('keeps a late previous-owner response out of the sidebar and detail', async () => {
    api.pathname = '/a';
    let resolveOldJob!: (job: AgentJobApi) => void;
    let resolveOldThread!: (thread: {
      thread_id: string;
      title: string;
      jobs: AgentJobApi[];
    }) => void;
    api.get.mockReturnValueOnce(
      new Promise<AgentJobApi>((resolve) => {
        resolveOldJob = resolve;
      }),
    );
    api.thread.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOldThread = resolve;
      }),
    );
    const app = () => (
      <AgentReadProvider userId={api.user}>
        <AgentsSidebar />
        <DetailRequestReader key={api.user} />
      </AgentReadProvider>
    );
    const view = render(app());
    expect(api.get).toHaveBeenCalledOnce();
    expect(api.thread).toHaveBeenCalledOnce();

    const bobJob = { ...base[0].job, title: 'Bob task', title_source: 'manual' } as AgentJobApi;
    api.get.mockResolvedValue(bobJob);
    api.thread.mockResolvedValue({ thread_id: 'thread-a', title: 'Bob task', jobs: [bobJob] });
    api.list.mockResolvedValue({
      conversations: [{ ...base[0], job: bobJob, title: 'Bob task' }],
      next_cursor: null,
    });
    api.user = 'bob';
    view.rerender(app());
    await waitFor(() =>
      expect(screen.getByTestId('detail-request-reader')).toHaveTextContent('Bob task'),
    );
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(api.thread).toHaveBeenCalledTimes(2);
    await screen.findByRole('link', { name: 'Bob task' });

    const aliceJob = { ...base[0].job, title: 'Alice task', title_source: 'manual' } as AgentJobApi;
    await act(async () => {
      resolveOldJob(aliceJob);
      resolveOldThread({ thread_id: 'thread-a', title: 'Alice task', jobs: [aliceJob] });
    });
    expect(screen.getByTestId('detail-request-reader')).toHaveTextContent('Bob task');
    expect(screen.queryByText('Alice task')).not.toBeInTheDocument();
  });

  it('shares startup and navigation metadata while preserving the initial owner resync', async () => {
    api.pathname = '/a';
    const job = { ...base[0].job, title_source: 'manual' };
    let resolveJob!: (job: AgentJobApi) => void;
    let resolveThread!: (thread: { thread_id: string; title: string; jobs: AgentJobApi[] }) => void;
    api.get.mockReturnValueOnce(
      new Promise<AgentJobApi>((resolve) => {
        resolveJob = resolve;
      }),
    );
    api.thread.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveThread = resolve;
      }),
    );
    const app = () => (
      <AgentReadProvider userId={api.user}>
        <AgentsSidebar />
        <DetailRequestReader />
      </AgentReadProvider>
    );
    const view = render(app());
    expect(api.get).toHaveBeenCalledOnce();
    expect(api.thread).toHaveBeenCalledOnce();
    expect(api.events).not.toHaveBeenCalled();
    await act(async () => resolveJob(job as AgentJobApi));
    expect(api.events).not.toHaveBeenCalled();
    await act(async () =>
      resolveThread({ thread_id: 'thread-a', title: 'Task a', jobs: [job as AgentJobApi] }),
    );
    await waitFor(() => expect(screen.getByTestId('detail-request-reader')).toHaveTextContent('a'));
    expect(api.events).toHaveBeenCalledOnce();
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(api.projects).toHaveBeenCalledOnce();

    api.get.mockImplementation(async (id: string) => ({
      ...base.find((row) => row.job.id === id)!.job,
      title_source: 'manual',
    }));
    api.thread.mockImplementation(async (id: string) => ({
      thread_id: `thread-${id}`,
      title: `Task ${id}`,
      jobs: [{ ...base.find((row) => row.job.id === id)!.job, title_source: 'manual' }],
    }));
    act(() => receiveSync());
    await waitFor(() => expect(api.projects).toHaveBeenCalledTimes(2));
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(api.thread).toHaveBeenCalledOnce();
    expect(api.list).toHaveBeenCalledTimes(4);

    api.pathname = '/b';
    view.rerender(app());
    await waitFor(() => expect(screen.getByTestId('detail-request-reader')).toHaveTextContent('b'));
    expect(api.get).toHaveBeenCalledTimes(3);
    expect(api.thread).toHaveBeenCalledTimes(2);
    expect(api.events).toHaveBeenCalledTimes(2);
    expect(api.list).toHaveBeenCalledTimes(6);
    expect(api.projects).toHaveBeenCalledTimes(3);
  });

  it.each(['project', 'flat'] as const)(
    'keeps the %s sidebar steady while archiving, navigating and receiving the archive push',
    async (group) => {
      localStorage.setItem(
        sidebarPreferencesKey('alice'),
        JSON.stringify({ ...DEFAULT_PREFERENCES, group }),
      );
      api.pathname = '/a';
      let archived = false;
      const pinned = conversation('pinned');
      pinned.job.pinned_at = '2026-09-10T00:00:00Z';
      const refreshes: (() => void)[] = [];
      api.list.mockImplementation(
        ({ pinned: pins }: { pinned?: boolean }) =>
          new Promise((resolve) => {
            const complete = () =>
              resolve({
                conversations: pins ? [pinned] : archived ? [base[1]] : base,
                next_cursor: null,
              });
            if (archived) refreshes.push(complete);
            else complete();
          }),
      );
      api.get.mockResolvedValue(base[0].job);
      api.thread.mockImplementation(async () => ({
        jobs: [base[0].job],
        title: base[0].title,
        archived_at: archived ? '2026-09-10T00:00:00Z' : null,
      }));
      api.archive.mockImplementation(async () => {
        archived = true;
        announceAgentSessionsChanged({ threadIds: ['thread-a'], archived: true });
        return { thread_id: 'thread-a', archived: true };
      });
      const view = render(<AgentsSidebar />);
      const remaining = await screen.findByRole('link', {
        name: group === 'flat' ? 'Task b · owner/repo' : 'Task b',
      });
      const pin = await screen.findByRole('link', { name: 'Task pinned · owner/repo' });
      await waitFor(() => expect(screen.queryAllByText('Loading…')).toHaveLength(0));
      const expectSteady = () => {
        expect(remaining).toBeInTheDocument();
        expect(pin).toBeInTheDocument();
        expect(screen.queryByText('Task a')).not.toBeInTheDocument();
        expect(screen.queryAllByText('Loading…')).toHaveLength(0);
      };

      fireEvent.click(screen.getByRole('button', { name: 'Archive Task a' }));
      await waitFor(() => expect(api.replace).toHaveBeenCalledWith('/'));
      expect(refreshes.length).toBeGreaterThan(0);
      expectSteady();

      api.pathname = '/';
      view.rerender(<AgentsSidebar />);
      expectSteady();
      await act(async () => refreshes.splice(0).forEach((complete) => complete()));
      expectSteady();

      vi.useFakeTimers();
      act(() => {
        receiveThreadChange({ id: 'thread-a', revision: 1, archived_at: '2026-09-10T00:00:00Z' });
        vi.advanceTimersByTime(PUSH_BATCH_MS);
      });
      expect(refreshes.length).toBeGreaterThan(0);
      expectSteady();
      await act(async () => refreshes.splice(0).forEach((complete) => complete()));
      expectSteady();
    },
  );

  it('keeps project and conversation lists steady during tool and usage updates', async () => {
    render(<AgentsSidebar />);
    await screen.findByText('Task a');
    api.list.mockClear();
    api.projects.mockClear();
    vi.useFakeTimers();

    for (const kind of ['usage', 'mcp', 'queue', 'publication'] as const) {
      act(() => {
        receiveHint(kind, kind === 'queue' || kind === 'publication' ? 'thread-a' : 'a');
        vi.advanceTimersByTime(PUSH_BATCH_MS);
      });
      expect(api.list).not.toHaveBeenCalled();
      expect(api.projects).not.toHaveBeenCalled();
      expect(screen.getByText('Task a')).toBeInTheDocument();
      expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
    }
  });

  it.each(['job', 'thread', 'resync'] as const)(
    'refreshes project and conversation lists for a %s change alongside tool activity',
    async (kind) => {
      render(<AgentsSidebar />);
      await screen.findByText('Task a');
      api.list.mockClear();
      api.projects.mockClear();
      vi.useFakeTimers();

      await act(async () => {
        receiveHint('usage', 'a');
        receiveHint('mcp', 'a');
        if (kind === 'job')
          receiveJobChange({ id: 'a', thread_id: 'thread-a', revision: 1, state: 'running' });
        else if (kind === 'thread')
          receiveThreadChange({ id: 'thread-a', revision: 1, title: 'Updated task' });
        else receiveSync();
        vi.advanceTimersByTime(PUSH_BATCH_MS);
      });

      expect(api.projects).toHaveBeenCalledOnce();
      expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ repo: 'owner/repo' }));
      expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ pinned: true }));
    },
  );

  it('refreshes a delayed automatic title after the conversation is idle', async () => {
    localStorage.setItem(
      sidebarPreferencesKey('alice'),
      JSON.stringify({ ...DEFAULT_PREFERENCES, group: 'flat' }),
    );
    let row = conversation('a');
    api.list.mockImplementation(async ({ pinned }: { pinned?: boolean }) => ({
      conversations: pinned ? [] : [row],
      next_cursor: null,
    }));
    render(<AgentsSidebar />);
    await screen.findByRole('link', { name: 'Task a · owner/repo' });
    row = { ...row, title: 'Export CSV' };
    act(() =>
      announceAgentSessionsChanged({
        threadIds: ['thread-a'],
        title: row.title,
        titleSource: 'generated',
      }),
    );
    expect(
      await screen.findByRole('link', { name: 'Export CSV · owner/repo' }),
    ).toBeInTheDocument();
  });

  it('keeps renamed and pinned conversations visible after metadata updates', async () => {
    localStorage.setItem(
      sidebarPreferencesKey('alice'),
      JSON.stringify({ ...DEFAULT_PREFERENCES, group: 'flat' }),
    );
    api.pathname = '/a';
    let row = conversation('a');
    api.get.mockImplementation(async () => row.job);
    api.thread.mockImplementation(async () => ({
      id: 'thread-a',
      title: row.title,
      jobs: [row.job],
    }));
    api.list.mockImplementation(async ({ pinned }: { pinned?: boolean }) => ({
      conversations: Boolean(row.job.pinned_at) === Boolean(pinned) ? [row] : [],
      next_cursor: null,
    }));
    render(<AgentsSidebar />);
    await screen.findByRole('link', { name: 'Task a · owner/repo' });
    row = { ...row, title: 'Renamed task' };
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-a'], title: row.title }));
    expect(
      await screen.findByRole('link', { name: 'Renamed task · owner/repo' }),
    ).toBeInTheDocument();
    row = { ...row, job: { ...row.job, pinned_at: '2026-09-10T00:00:00Z' } };
    act(() =>
      announceAgentSessionsChanged({ threadIds: ['thread-a'], pinnedAt: row.job.pinned_at }),
    );
    const pinned = await screen.findByRole('region', { name: 'Pinned' });
    await waitFor(() =>
      expect(
        within(pinned).getByRole('link', { name: 'Renamed task · owner/repo' }),
      ).toBeInTheDocument(),
    );
    row = { ...row, job: { ...row.job, pinned_at: null } };
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-a'], pinnedAt: null }));
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Pinned' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('link', { name: 'Renamed task · owner/repo' })).toBeInTheDocument();
  });

  it('adds an empty authorized workspace, exits flat search and restores it without duplicates', async () => {
    localStorage.setItem(
      sidebarPreferencesKey('alice'),
      JSON.stringify({ ...DEFAULT_PREFERENCES, group: 'flat' }),
    );
    api.config.mockResolvedValue({ repos: ['owner/empty', 'owner/repo'], github_connected: true });
    api.list.mockImplementation(async ({ repo }: { repo?: string }) => ({
      conversations: repo === 'owner/empty' ? [] : base,
      next_cursor: null,
    }));
    const view = render(<AgentsSidebar />);
    await screen.findByRole('link', { name: 'Task a · owner/repo' });
    fireEvent.click(screen.getByRole('button', { name: 'Search tasks' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Search tasks' }), {
      target: { value: 'anything' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add workspace' }));
    fireEvent.click(await screen.findByRole('button', { name: 'owner/empty' }));
    expect(screen.queryByRole('textbox', { name: 'Search tasks' })).not.toBeInTheDocument();
    expect(screen.getByTitle('owner/empty')).toHaveAttribute('aria-expanded', 'true');
    const saved = JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!);
    expect(saved.group).toBe('project');
    expect(saved.addedProjects).toEqual(['owner/empty']);
    expect(saved.manualInitialized).toBe(false);
    view.unmount();
    render(<AgentsSidebar />);
    expect(await screen.findByTitle('owner/empty')).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Add workspace' }));
    fireEvent.click(await screen.findByRole('button', { name: 'owner/empty · Already added' }));
    expect(screen.getAllByTitle('owner/empty')).toHaveLength(1);
    expect(JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!).addedProjects).toEqual(
      ['owner/empty'],
    );
  });
  it('shows a spinner while running and clears completion unread dots when opened', async () => {
    const running = conversation('a');
    running.job.state = 'running';
    api.list.mockResolvedValue({ conversations: [running, base[1]], next_cursor: null });
    const { rerender } = render(<AgentsSidebar />);
    await screen.findByRole('link', { name: 'Task a' });
    expect(screen.getAllByLabelText('Running')).toHaveLength(1);
    expect(screen.getAllByLabelText('Unread result')).toHaveLength(1);
    fireEvent.click(screen.getByRole('link', { name: 'Task b' }));
    await waitFor(() => expect(screen.queryByLabelText('Unread result')).not.toBeInTheDocument());
    api.list.mockResolvedValue({ conversations: base, next_cursor: null });
    api.pathname = '/integrations';
    rerender(<AgentsSidebar />);
    await waitFor(() => expect(screen.queryByLabelText('Running')).not.toBeInTheDocument());
    expect(screen.getAllByLabelText('Unread result')).toHaveLength(1);
    api.pathname = '/a';
    rerender(<AgentsSidebar />);
    await waitFor(() => expect(screen.queryByLabelText('Unread result')).not.toBeInTheDocument());
    api.pathname = '/';
    rerender(<AgentsSidebar />);
    expect(screen.queryByLabelText('Unread result')).not.toBeInTheDocument();
  });

  it('keeps the project highlighted when a loaded sibling session is awaiting detail', async () => {
    api.pathname = '/a';
    api.get.mockImplementation((id: string) =>
      id === 'a' ? Promise.resolve(base[0].job) : new Promise(() => {}),
    );
    const { rerender } = render(<AgentsSidebar />);
    await waitFor(() =>
      expect(screen.getByTitle('owner/repo')).toHaveAttribute('aria-current', 'true'),
    );
    await screen.findByRole('link', { name: 'Task b' });
    api.pathname = '/b';
    rerender(<AgentsSidebar />);
    expect(screen.getByTitle('owner/repo')).toHaveAttribute('aria-current', 'true');
    expect(screen.getByRole('link', { name: 'Task b' })).toHaveAttribute('aria-current', 'page');
  });

  it('switches projects immediately for a known session without guessing unknown or other-owner routes', async () => {
    const other = conversation('c', 'owner/other');
    api.projects.mockResolvedValue([
      { repo: 'owner/repo', task_count: 2, active_count: 0 },
      { repo: 'owner/other', task_count: 1, active_count: 0 },
    ]);
    api.list.mockImplementation(async ({ repo }: { repo?: string }) => ({
      conversations: repo === 'owner/other' ? [other] : base,
      next_cursor: null,
    }));
    api.get.mockImplementation(() => new Promise(() => {}));
    const { rerender } = render(<AgentsSidebar />);
    await screen.findByRole('link', { name: 'Task a' });
    fireEvent.click(screen.getByTitle('owner/other'));
    await screen.findByRole('link', { name: 'Task c' });
    api.pathname = '/a';
    rerender(<AgentsSidebar />);
    expect(screen.getByTitle('owner/repo')).toHaveAttribute('aria-current', 'true');
    api.pathname = '/c';
    rerender(<AgentsSidebar />);
    expect(screen.getByTitle('owner/other')).toHaveAttribute('aria-current', 'true');
    expect(screen.getByTitle('owner/repo')).not.toHaveAttribute('aria-current');
    api.pathname = '/unloaded';
    rerender(<AgentsSidebar />);
    expect(screen.getByTitle('owner/other')).not.toHaveAttribute('aria-current');
    api.user = 'bob';
    api.pathname = '/c';
    api.list.mockImplementation(() => new Promise(() => {}));
    rerender(<AgentsSidebar />);
    expect(screen.getByTitle('owner/other')).not.toHaveAttribute('aria-current');
  });

  it('has exactly four menu options, applies immediately, and returns keyboard focus', async () => {
    render(<AgentsSidebar />);
    await screen.findByText('Task a');
    const trigger = screen.getByRole('button', { name: 'View options' });
    fireEvent.click(trigger);
    const menu = screen.getByRole('menu');
    expect(within(menu).getAllByRole('menuitemradio')).toHaveLength(4);
    expect(
      within(menu)
        .getAllByRole('menuitemradio')
        .filter((el) => el.getAttribute('aria-checked') === 'true'),
    ).toHaveLength(2);
    expect(screen.getByRole('menuitemradio', { name: 'Project' })).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitemradio', { name: 'In one list' })).toHaveFocus();
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'In one list' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole('button', { name: 'repo' })).not.toBeInTheDocument();
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    fireEvent.click(trigger);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
  it('highlights repo query on new tasks without falsely highlighting the first project', async () => {
    const { rerender } = render(<AgentsSidebar />);
    await screen.findByText('Task a');
    expect(screen.queryByText('Current')).not.toBeInTheDocument();
    api.params = 'repo=owner%2Frepo';
    rerender(<AgentsSidebar />);
    expect(screen.getByTitle('owner/repo')).toHaveAttribute('aria-current', 'true');
    expect(screen.queryByText('Current')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'repo' }));
    expect(screen.getByTitle('owner/repo')).toHaveAttribute('aria-current', 'true');
    expect(screen.queryByText('Current')).not.toBeInTheDocument();
  });
  it('resolves historical turns through detail and selects canonical latest thread in flat mode', async () => {
    localStorage.setItem(
      sidebarPreferencesKey('alice'),
      JSON.stringify({ ...DEFAULT_PREFERENCES, group: 'flat' }),
    );
    api.pathname = '/old-turn';
    const latest = conversation('latest');
    latest.job.thread_id = 'old-thread';
    latest.job.turn_no = 7;
    api.get.mockResolvedValue({ ...latest.job, id: 'old-turn', turn_no: 1 });
    api.thread.mockResolvedValue({
      thread_id: 'old-thread',
      title: 'Original canonical title',
      archived_at: null,
      jobs: [latest.job],
      messages: [],
    });
    render(<AgentsSidebar />);
    const selected = await screen.findByRole('link', {
      name: 'Original canonical title · owner/repo',
    });
    expect(selected).toHaveAttribute('aria-current', 'page');
    expect(selected).toHaveAttribute('href', '/latest');
  });
  it('does not inject an archived historical selection into the normal tree', async () => {
    api.pathname = '/archived-turn';
    const archived = conversation('archived-turn');
    api.get.mockResolvedValue(archived.job);
    api.thread.mockResolvedValue({
      thread_id: archived.job.thread_id,
      title: 'Archived only',
      archived_at: '2026-09-02T00:00:00Z',
      jobs: [archived.job],
      messages: [],
    });
    render(<AgentsSidebar />);
    await waitFor(() =>
      expect(screen.getByTitle('owner/repo')).toHaveAttribute('aria-current', 'true'),
    );
    expect(screen.queryByText('Archived only')).not.toBeInTheDocument();
  });
  it('does not inject a deleted selection when its initial detail response finishes late', async () => {
    api.pathname = '/deleted';
    const deleted = conversation('deleted');
    let resolveOld!: (value: AgentJobApi) => void;
    api.get.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    api.thread.mockResolvedValue({
      thread_id: deleted.job.thread_id,
      title: 'Deleted conversation',
      jobs: [deleted.job],
      messages: [],
    });
    render(<AgentsSidebar />);
    await screen.findByText('Task a');
    act(() => announceAgentSessionsChanged({ threadIds: [deleted.job.thread_id!], deleted: true }));
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    await act(async () => resolveOld(deleted.job));
    expect(screen.queryByText('Deleted conversation')).not.toBeInTheDocument();
    expect(screen.queryByText('Current')).not.toBeInTheDocument();
  });
  it('removes a deleted selection from the remembered navigation rows', async () => {
    api.pathname = '/deleted';
    const deleted = conversation('deleted');
    api.get.mockResolvedValue(deleted.job);
    api.thread.mockResolvedValue({
      thread_id: deleted.job.thread_id,
      title: 'Deleted conversation',
      jobs: [deleted.job],
      messages: [],
    });
    render(<AgentsSidebar />);
    await screen.findByText('Deleted conversation');
    api.get.mockRejectedValue(new Error('Session no longer available'));
    act(() => announceAgentSessionsChanged({ threadIds: [deleted.job.thread_id!], deleted: true }));
    await waitFor(() => expect(screen.queryByText('Deleted conversation')).not.toBeInTheDocument());
    expect(screen.queryByText('Current')).not.toBeInTheDocument();
  });

  it.each([{ ctrlKey: true }, { metaKey: true }])(
    'preserves a portaled modal focus cycle when search is requested with %j',
    async (modifier) => {
      function Workspace() {
        const [open, setOpen] = useState(true);
        return open ? (
          <SkillDialog title="Skills" onClose={() => setOpen(false)}>
            <input aria-label="Find skills" />
          </SkillDialog>
        ) : null;
      }
      render(
        <>
          <AgentsSidebar />
          <Workspace />
        </>,
      );
      await screen.findByText('Task a');
      const input = screen.getByRole('textbox', { name: 'Find skills' });
      expect(input).toHaveFocus();

      fireEvent.keyDown(input, { key: 'k', ...modifier });
      await act(async () => {
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      });

      expect(input).toHaveFocus();
      expect(screen.queryByRole('textbox', { name: 'Search tasks' })).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Search tasks' })).toHaveAttribute(
        'aria-expanded',
        'false',
      );

      fireEvent.keyDown(input, { key: 'Tab' });
      expect(screen.getByRole('button', { name: 'Close skill dialog' })).toHaveFocus();
      fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

      fireEvent.keyDown(document, { key: 'k', ...modifier });
      await waitFor(() =>
        expect(screen.getByRole('textbox', { name: 'Search tasks' })).toHaveFocus(),
      );
    },
  );

  it('waits until a native dialog closes before moving focus to search', async () => {
    render(
      <>
        <AgentsSidebar />
        <dialog open aria-label="Task details">
          <input aria-label="Task title" />
        </dialog>
      </>,
    );
    await screen.findByText('Task a');
    const input = screen.getByRole('textbox', { name: 'Task title' });
    input.focus();

    fireEvent.keyDown(input, { key: 'k', ctrlKey: true });

    expect(input).toHaveFocus();
    expect(screen.queryByRole('textbox', { name: 'Search tasks' })).not.toBeInTheDocument();

    screen.getByRole('dialog', { name: 'Task details' }).removeAttribute('open');
    fireEvent.keyDown(document, { key: 'k', ctrlKey: true });
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Search tasks' })).toHaveFocus(),
    );
  });

  it('can search while an inactive modal remains hidden', async () => {
    render(
      <>
        <AgentsSidebar />
        <div hidden>
          <div role="dialog" aria-modal="true" aria-label="Inactive dialog" />
        </div>
      </>,
    );
    await screen.findByText('Task a');

    fireEvent.keyDown(document, { key: 'k', ctrlKey: true });

    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Search tasks' })).toHaveFocus(),
    );
  });

  it('searches server history including archived results, escapes highlights, and restores grouping', async () => {
    const archived = { ...conversation('old'), archived: true, title: 'Old <img> task' };
    api.list.mockImplementation(
      async ({ q, includeArchived }: { q?: string; includeArchived?: boolean }) => ({
        conversations: q ? (includeArchived ? [archived] : []) : base,
        next_cursor: null,
      }),
    );
    render(<AgentsSidebar />);
    await screen.findByText('Task a');
    fireEvent.keyDown(document, { key: 'k', ctrlKey: true });
    const input = screen.getByRole('textbox', { name: 'Search tasks' });
    fireEvent.change(input, { target: { value: '<img>' } });
    await screen.findByText('No matching tasks. Try another title or repository.');
    fireEvent.click(screen.getByRole('button', { name: 'Include archived' }));
    const match = await screen.findByText('<img>');
    expect(match.tagName).toBe('MARK');
    expect(document.querySelector('img')).not.toBeInTheDocument();
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: '<img>', includeArchived: true }),
    );
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(screen.getByRole('link', { name: /Old <img> task/ })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    await screen.findByText('Task a');
    expect(screen.getByRole('button', { name: 'repo' })).toHaveAttribute('aria-expanded', 'true');
  });
  it('keeps manual task and project movement accessible and remembered across modes', async () => {
    render(<AgentsSidebar />);
    await screen.findByText('Task a');
    fireEvent.click(screen.getByRole('button', { name: 'View options' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Manual' }));
    const row = screen.getByRole('link', { name: 'Task b' });
    row.focus();
    fireEvent.keyDown(row, { key: 'ArrowUp', altKey: true });
    expect(
      screen
        .getAllByRole('link')
        .filter((link) => link.textContent?.startsWith('Task'))
        .map((link) => link.textContent),
    ).toEqual(['Task b', 'Task a']);
    const saved = JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!);
    expect(saved.tasks['repo:owner/repo']).toEqual(['thread-b', 'thread-a']);
    fireEvent.click(screen.getByRole('button', { name: 'View options' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Last updated' }));
    fireEvent.click(screen.getByRole('button', { name: 'View options' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Manual' }));
    expect(
      screen
        .getAllByRole('link')
        .filter((link) => link.textContent?.startsWith('Task'))
        .map((link) => link.textContent),
    ).toEqual(['Task b', 'Task a']);
    await act(async () => {});
  });
  it('supports drag ordering within a repository and refuses cross-repository drops', async () => {
    const other = conversation('other', 'second/repo');
    api.projects.mockResolvedValue([
      {
        repo: 'owner/repo',
        task_count: 2,
        active_count: 0,
        last_activity_at: base[0].last_activity_at,
      },
      { repo: 'second/repo', task_count: 1, active_count: 0, last_activity_at: null },
    ]);
    api.list.mockImplementation(async ({ repo }: { repo?: string }) => ({
      conversations: repo === 'second/repo' ? [other] : base,
      next_cursor: null,
    }));
    render(<AgentsSidebar />);
    await screen.findByText('Task a');
    fireEvent.click(screen.getByRole('button', { name: 'View options' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Manual' }));
    const transfer = { effectAllowed: '', setData: vi.fn() };
    const a = screen.getByRole('link', { name: 'Task a' }).parentElement!;
    const b = screen.getByRole('link', { name: 'Task b' }).parentElement!;
    fireEvent.dragStart(b, { dataTransfer: transfer });
    fireEvent.dragOver(a);
    fireEvent.drop(a);
    expect(
      JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!).tasks['repo:owner/repo'],
    ).toEqual(['thread-b', 'thread-a']);
    fireEvent.click(screen.getAllByRole('button', { name: 'repo' })[1]);
    const otherLink = await screen.findByRole('link', { name: 'Task other' });
    fireEvent.dragStart(b, { dataTransfer: transfer });
    fireEvent.drop(otherLink.parentElement!);
    expect(otherLink).toHaveAttribute('href', '/other');
    expect(
      JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!).tasks['repo:second/repo'],
    ).toEqual(['thread-other']);
  });
  it('can search a just-archived conversation when archived results are included', async () => {
    let archived = false;
    api.archive.mockImplementation(async () => {
      archived = true;
    });
    api.list.mockImplementation(
      async ({ q, includeArchived }: { q?: string; includeArchived?: boolean }) => ({
        conversations:
          q && includeArchived ? [{ ...base[0], archived: true }] : archived ? [base[1]] : base,
        next_cursor: null,
      }),
    );
    render(<AgentsSidebar />);
    await screen.findByText('Task a');
    fireEvent.click(screen.getByRole('button', { name: 'Archive Task a' }));
    await waitFor(() => expect(screen.queryByText('Task a')).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Search tasks' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Task a' } });
    fireEvent.click(screen.getByRole('button', { name: 'Include archived' }));
    expect(
      await screen.findByRole('link', { name: 'Task a · owner/repo · Archived' }),
    ).toHaveAttribute('href', '/a');
  });
  it('loads pins and only expanded project pages, and records no history IDs before manual order', async () => {
    render(<AgentsSidebar />);
    await screen.findByText('Task a');
    expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ repo: 'owner/repo' }));
    expect(
      api.list.mock.calls.every(
        ([options]) => options.pinned === true || options.repo === 'owner/repo',
      ),
    ).toBe(true);
    expect(api.list).toHaveBeenCalledWith(
      expect.objectContaining({ pinned: true, repo: undefined }),
    );
    expect(localStorage.getItem(sidebarPreferencesKey('alice'))).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'View options' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'In one list' }));
    await screen.findByRole('link', { name: 'Task a · owner/repo' });
    const saved = JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!);
    expect(saved.tasks).toBeUndefined();
    expect(saved.projects).toBeUndefined();
    fireEvent.click(screen.getByRole('button', { name: 'View options' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Manual' }));
    const manual = JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!);
    expect(manual.tasks.flat).toEqual(['thread-a', 'thread-b']);
    expect(manual.projects).toEqual(['owner/repo']);
  });
  it('restores an old hidden thread directly into search and its project without a global-page hit', async () => {
    let archived = false;
    const old = conversation('old');
    old.job.task_prompt = old.title;
    old.last_activity_at = '2025-01-01T00:00:00Z';
    api.archive.mockImplementation(async () => {
      archived = true;
    });
    api.setArchived.mockImplementation(async (ids: string[], value: boolean) => {
      archived = value;
      announceAgentSessionsChanged({ threadIds: ids, archived: value });
      return {
        sessions: ids.map((thread_id) => ({
          thread_id,
          archived: value,
          archived_at: value ? '2026-09-01T00:00:00Z' : null,
        })),
      };
    });
    api.sessions.mockImplementation(async (options: AgentSessionListOptions) => {
      const archivedSession = {
        thread_id: old.job.thread_id,
        title: old.title,
        job: old.job,
        can_delete: true,
        archived_at: '2026-09-01T00:00:00Z',
      };
      const sessions = archived ? [archivedSession] : [];
      return {
        sessions,
        total: options.includeSummary === false ? null : sessions.length,
        projects:
          options.includeSummary === false
            ? null
            : archived
              ? [{ repo: old.job.repo, count: 1 }]
              : [],
        next_cursor: null,
      };
    });
    api.list.mockImplementation(async ({ q, repo }: { q?: string; repo?: string }) => ({
      // This old task never belongs to the unscoped first page.
      conversations: q
        ? archived
          ? []
          : [old]
        : repo
          ? [...base, ...(archived ? [] : [old])]
          : base,
      next_cursor: null,
    }));
    const { rerender } = render(
      <>
        <AgentsSidebar />
      </>,
    );
    await screen.findByText('Task old');
    fireEvent.click(screen.getByRole('button', { name: 'Archive Task old' }));
    await waitFor(() => expect(screen.queryByText('Task old')).not.toBeInTheDocument());
    rerender(
      <>
        <AgentsSidebar />
        <ArchivedTasksView />
      </>,
    );
    await screen.findByRole('button', { name: 'Unarchive Task old' });
    fireEvent.click(screen.getByRole('button', { name: 'Search tasks' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Task old' } });
    await screen.findByText('No matching tasks. Try another title or repository.');
    const queriesBeforeRestore = api.projects.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: 'Unarchive Task old' }));
    await screen.findByRole('link', { name: 'Task old · owner/repo' });
    expect(api.projects.mock.calls.length).toBeGreaterThan(queriesBeforeRestore);
    fireEvent.click(screen.getByRole('button', { name: 'Close search' }));
    expect(await screen.findByRole('link', { name: 'Task old' })).toHaveAttribute('href', '/old');
    expect(old.last_activity_at).toBe('2025-01-01T00:00:00Z');
  });
  it('ignores restore invalidation from a different signed-in account', async () => {
    render(<AgentsSidebar />);
    await screen.findByText('Task a');
    api.list.mockClear();
    api.projects.mockClear();
    act(() => announceConversationRestored({ threadId: 'thread-a', userId: 'bob' }));
    expect(api.list).not.toHaveBeenCalled();
    expect(api.projects).not.toHaveBeenCalled();
    act(() => announceConversationRestored({ threadId: 'thread-a', userId: 'alice' }));
    await waitFor(() => expect(api.projects).toHaveBeenCalledOnce());
    expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ repo: 'owner/repo' }));
  });
});
