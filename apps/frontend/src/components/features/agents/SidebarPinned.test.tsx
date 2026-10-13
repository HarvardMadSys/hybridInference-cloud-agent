// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentConversationApi, AgentJobApi } from '@/lib/api/agents';

const api = vi.hoisted(() => ({
  list: vi.fn(),
  projects: vi.fn(),
  pin: vi.fn(),
  unpin: vi.fn(),
  get: vi.fn(),
  thread: vi.fn(),
  pathname: '/',
}));
vi.mock('next/navigation', () => ({
  usePathname: () => api.pathname,
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ replace: vi.fn() }),
}));
vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useAuth: () => ({ state: { user: { id: 'pins-user' } } }),
    useT: () => (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
      translate('en', key, vars),
  };
});
vi.mock('@/lib/api/agents', () => ({
  listAgentConversations: api.list,
  listAgentProjects: api.projects,
  pinAgentJob: api.pin,
  unpinAgentJob: api.unpin,
  archiveAgentJob: vi.fn(),
  getAgentJob: api.get,
  getAgentJobThread: api.thread,
}));
import { AgentsSidebar } from './AgentsSidebar';
import { DEFAULT_PREFERENCES, sidebarPreferencesKey } from './sidebarPreferences';

function conversation(id: string, repo: string, pinned: boolean): AgentConversationApi {
  return {
    job: {
      id,
      thread_id: id,
      repo,
      state: 'succeeded',
      task_prompt: id,
      pinned_at: pinned ? '2026-09-10T00:00:00Z' : null,
    } as AgentJobApi,
    title: id,
    archived: false,
    last_activity_at: '2026-09-10T00:00:00Z',
  };
}
let rows: AgentConversationApi[];
beforeEach(() => {
  localStorage.clear();
  api.pathname = '/';
  api.get.mockRejectedValue(new Error('No selection'));
  api.thread.mockResolvedValue(null);
  rows = [
    conversation('Pinned quiet task', 'owner/quiet', true),
    conversation('Regular task', 'owner/recent', false),
  ];
  api.projects.mockResolvedValue([
    { repo: 'owner/recent', task_count: 1, active_count: 0, last_activity_at: '2026-09-10' },
    { repo: 'owner/quiet', task_count: 1, active_count: 0, last_activity_at: '2026-01-01' },
  ]);
  api.list.mockImplementation(
    async ({ pinned, repo, q }: { pinned?: boolean; repo?: string; q?: string }) => ({
      conversations: rows
        .filter(
          (row) =>
            (pinned === undefined || Boolean(row.job.pinned_at) === pinned) &&
            (!repo || row.job.repo === repo) &&
            (!q || row.title.includes(q)),
        )
        .map((row) => ({ ...row, job: { ...row.job } })),
      next_cursor: null,
    }),
  );
  api.pin.mockImplementation(async (id: string) => {
    rows.find((row) => row.job.id === id)!.job.pinned_at = '2026-09-10T12:00:00Z';
    return { thread_id: id, pinned: true, pinned_at: '2026-09-10T12:00:00Z' };
  });
  api.unpin.mockImplementation(async (id: string) => {
    rows.find((row) => row.job.id === id)!.job.pinned_at = null;
    return { thread_id: id, pinned: false, pinned_at: null };
  });
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('standalone pinned section', () => {
  it('shares manual pin order across projects and grouping modes', async () => {
    rows.push(conversation('Another pin', 'owner/recent', true));
    render(<AgentsSidebar />);
    await screen.findByRole('link', { name: /Another pin/ });
    fireEvent.click(screen.getByRole('button', { name: 'View options' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Manual' }));
    const order = () =>
      within(screen.getByRole('region', { name: 'Pinned' }))
        .getAllByRole('link')
        .map((link) => link.getAttribute('href'));
    expect(order()).toEqual(['/Pinned quiet task', '/Another pin']);
    fireEvent.keyDown(screen.getByRole('link', { name: /Another pin/ }), {
      key: 'ArrowUp',
      altKey: true,
    });
    expect(order()).toEqual(['/Another pin', '/Pinned quiet task']);
    expect(
      JSON.parse(localStorage.getItem(sidebarPreferencesKey('pins-user'))!).tasks.pinned,
    ).toEqual(['Another pin', 'Pinned quiet task']);
    fireEvent.click(screen.getByRole('button', { name: 'View options' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'In one list' }));
    expect(order()).toEqual(['/Another pin', '/Pinned quiet task']);
    fireEvent.click(screen.getByRole('button', { name: 'View options' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Project' }));
    expect(order()).toEqual(['/Another pin', '/Pinned quiet task']);
  });

  it('does not reinsert a selected task when its old detail resolves after pinning', async () => {
    api.pathname = '/Regular task';
    const oldJob = { ...rows[1].job };
    let resolveDetail!: (job: AgentJobApi) => void;
    api.get.mockImplementation(
      () =>
        new Promise<AgentJobApi>((resolve) => {
          resolveDetail = resolve;
        }),
    );
    render(<AgentsSidebar />);
    await screen.findByRole('button', { name: 'Pin Regular task' });
    fireEvent.click(screen.getByRole('button', { name: 'Pin Regular task' }));
    await screen.findByRole('button', { name: 'Unpin Regular task' });
    await act(async () => resolveDetail(oldJob));
    const selected = screen.getByRole('link', { name: /Regular task/ });
    expect(selected).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('region', { name: 'Pinned' })).toContainElement(selected);
    expect(screen.getAllByRole('link', { name: /Regular task/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Unpin Regular task' }));
    await screen.findByRole('button', { name: 'Pin Regular task' });
    await waitFor(() =>
      expect(document.getElementById('project-owner%2Frecent')).toContainElement(
        screen.getByRole('link', { name: 'Regular task' }),
      ),
    );
    expect(screen.getAllByRole('link', { name: /Regular task/ })).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'Regular task' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('loads further pins independently of project history', async () => {
    rows = Array.from({ length: 51 }, (_, index) =>
      conversation(`Pin ${index + 1}`, 'owner/quiet', true),
    );
    api.list.mockImplementation(
      async ({ pinned, cursor }: { pinned?: boolean; cursor?: string }) => ({
        conversations: pinned ? rows.slice(cursor ? 50 : 0, cursor ? 51 : 50) : [],
        next_cursor: pinned && !cursor ? 'next-pins' : null,
      }),
    );
    render(<AgentsSidebar />);
    await screen.findByRole('link', { name: /^Pin 1 ·/ });
    expect(screen.queryByRole('link', { name: /Pin 51/ })).not.toBeInTheDocument();
    for (let index = 0; index < 3; index += 1)
      fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
    await screen.findByRole('link', { name: /Pin 51/ });
    expect(api.list).toHaveBeenCalledWith(
      expect.objectContaining({ pinned: true, cursor: 'next-pins' }),
    );
    expect(screen.getByTitle('owner/quiet')).toHaveAttribute('aria-expanded', 'false');
  });

  it('shows pins from collapsed projects above Projects exactly once', async () => {
    render(<AgentsSidebar />);
    const pinned = await screen.findByText('Pinned');
    const task = await screen.findByRole('link', { name: /Pinned quiet task/ });
    expect(
      pinned.compareDocumentPosition(screen.getByText('Projects')) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByTitle('owner/quiet')).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(screen.getByTitle('owner/quiet'));
    await waitFor(() =>
      expect(api.list).toHaveBeenCalledWith(
        expect.objectContaining({ repo: 'owner/quiet', pinned: false }),
      ),
    );
    expect(screen.getAllByRole('link', { name: /Pinned quiet task/ })).toHaveLength(1);
    expect(document.getElementById('project-owner%2Fquiet')).not.toContainElement(task);
  });

  it('moves tasks on pin and unpin and removes the empty section', async () => {
    render(<AgentsSidebar />);
    await screen.findByRole('button', { name: 'Pin Regular task' });
    fireEvent.click(screen.getByRole('button', { name: 'Pin Regular task' }));
    await screen.findByRole('button', { name: 'Unpin Regular task' });
    expect(document.getElementById('project-owner%2Frecent')).not.toContainElement(
      screen.getByRole('link', { name: /Regular task/ }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Unpin Regular task' }));
    await waitFor(() =>
      expect(
        within(document.getElementById('project-owner%2Frecent')!).getByRole('link', {
          name: 'Regular task',
        }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Unpin Pinned quiet task' }));
    await waitFor(() =>
      expect(screen.queryByText('Pinned', { exact: true })).not.toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTitle('owner/quiet'));
    await screen.findByRole('button', { name: 'Pin Pinned quiet task' });
  });

  it('keeps pins separate in flat mode and searches each task once', async () => {
    localStorage.setItem(
      sidebarPreferencesKey('pins-user'),
      JSON.stringify({ ...DEFAULT_PREFERENCES, group: 'flat' }),
    );
    render(<AgentsSidebar />);
    await screen.findByText('Pinned', { exact: true });
    await screen.findByRole('link', { name: /Regular task/ });
    expect(screen.getAllByRole('link', { name: /Pinned quiet task/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Search tasks' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Pinned quiet' } });
    await waitFor(() =>
      expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ q: 'Pinned quiet' })),
    );
    await waitFor(() =>
      expect(screen.getAllByRole('link', { name: /Pinned quiet task/ })).toHaveLength(1),
    );
    expect(screen.queryByText('Pinned', { exact: true })).not.toBeInTheDocument();
  });
});
