// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, createEvent, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJobApi } from '@/lib/api/agents';
const api = vi.hoisted(() => ({ list: vi.fn(), projects: vi.fn() }));
vi.mock('next/navigation', () => ({
  usePathname: () => '/',
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ replace: vi.fn() }),
}));
vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useAuth: () => ({ state: { user: { id: 'drag-user' } } }),
    useT: () => (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
      translate('en', key, vars),
  };
});
vi.mock('@/components/features/settings/SettingsLauncher', () => ({
  SettingsLauncher: () => null,
}));
vi.mock('@/lib/api/agents', () => ({
  listAgentConversations: api.list,
  listAgentProjects: api.projects,
  getAgentJob: vi.fn(),
  getAgentJobThread: vi.fn(),
  archiveAgentJob: vi.fn(),
  pinAgentJob: vi.fn(),
  unpinAgentJob: vi.fn(),
}));
import { AgentsSidebar } from './AgentsSidebar';
import { DEFAULT_PREFERENCES, sidebarPreferencesKey } from './sidebarPreferences';
function row(id: string, repo = 'owner/repo', pinned = false) {
  return {
    job: {
      id,
      thread_id: id,
      repo,
      state: 'succeeded',
      task_prompt: id,
      pinned_at: pinned ? '2026-09-01' : null,
    } as AgentJobApi,
    title: id,
    archived: false,
    last_activity_at: '2026-09-01T00:00:00Z',
  };
}
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(
    sidebarPreferencesKey('drag-user'),
    JSON.stringify({
      ...DEFAULT_PREFERENCES,
      order: 'manual',
      manualInitialized: true,
      projects: ['owner/repo'],
      tasks: { 'repo:owner/repo': ['first', 'second'] },
    }),
  );
  api.projects.mockResolvedValue([
    { repo: 'owner/repo', task_count: 2, active_count: 0, last_activity_at: null },
  ]);
  api.list.mockResolvedValue({ conversations: [row('first'), row('second')], next_cursor: null });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.resetAllMocks();
});
function transfer() {
  return { effectAllowed: '', dropEffect: '', clearData: vi.fn(), setData: vi.fn() };
}
function over(
  element: HTMLElement,
  type: 'dragOver' | 'drop',
  clientY: number,
  dataTransfer: ReturnType<typeof transfer>,
) {
  const event = createEvent[type](element, { dataTransfer });
  Object.defineProperty(event, 'clientY', { value: clientY });
  fireEvent(element, event);
}
function taskOrder() {
  return screen
    .getAllByRole('link')
    .filter((link) => ['first', 'second'].includes(link.textContent ?? ''))
    .map((link) => link.textContent);
}
describe('manual pointer reordering', () => {
  it('moves downward when dropped on the lower half of the next row', async () => {
    render(<AgentsSidebar />);
    const first = (await screen.findByRole('link', { name: 'first' })).parentElement!;
    const second = screen.getByRole('link', { name: 'second' }).parentElement!;
    vi.spyOn(second, 'getBoundingClientRect').mockReturnValue({
      top: 100,
      height: 32,
      bottom: 132,
    } as DOMRect);
    const dataTransfer = transfer();
    fireEvent.dragStart(first, { dataTransfer });
    over(second, 'dragOver', 125, dataTransfer);
    expect(second).toHaveAttribute('data-drop-position', 'after');
    over(second, 'drop', 125, dataTransfer);
    expect(taskOrder()).toEqual(['second', 'first']);
    expect(second).not.toHaveAttribute('data-drop-position');
  });
  it('moves upward from a grip and clears the insertion indicator after cancellation', async () => {
    render(<AgentsSidebar />);
    const first = (await screen.findByRole('link', { name: 'first' })).parentElement!;
    const second = screen.getByRole('link', { name: 'second' }).parentElement!;
    vi.spyOn(first, 'getBoundingClientRect').mockReturnValue({
      top: 100,
      height: 32,
      bottom: 132,
    } as DOMRect);
    const grip = second.querySelector<HTMLElement>('[data-drag-handle]')!;
    expect(grip).toHaveAttribute('title', 'Drag to reorder, or press Alt + Arrow Up / Down');
    const dataTransfer = transfer();
    fireEvent.pointerDown(grip);
    fireEvent.dragStart(second, { dataTransfer });
    over(first, 'dragOver', 105, dataTransfer);
    expect(first).toHaveAttribute('data-drop-position', 'before');
    fireEvent.dragEnd(second);
    expect(first).not.toHaveAttribute('data-drop-position');
    expect(taskOrder()).toEqual(['first', 'second']);
    fireEvent.pointerDown(grip);
    fireEvent.dragStart(second, { dataTransfer });
    over(first, 'drop', 105, dataTransfer);
    expect(taskOrder()).toEqual(['second', 'first']);
  });
  it('blocks action-button drag starts and prevents native URL dragging from task links', async () => {
    render(<AgentsSidebar />);
    const firstLink = await screen.findByRole('link', { name: 'first' });
    const first = firstLink.parentElement!;
    const second = screen.getByRole('link', { name: 'second' }).parentElement!;
    const dataTransfer = transfer();
    fireEvent.dragStart(firstLink, { dataTransfer });
    expect(dataTransfer.clearData).toHaveBeenCalledOnce();
    expect(dataTransfer.setData).toHaveBeenCalledWith('text/plain', 'first');
    fireEvent.dragEnd(firstLink);
    dataTransfer.setData.mockClear();
    const pin = screen.getByRole('button', { name: 'Pin first' });
    fireEvent.pointerDown(pin);
    const start = createEvent.dragStart(first, { dataTransfer });
    fireEvent(first, start);
    expect(start.defaultPrevented).toBe(true);
    expect(dataTransfer.setData).not.toHaveBeenCalled();
    over(second, 'drop', 125, dataTransfer);
    expect(taskOrder()).toEqual(['first', 'second']);
  });
  it('keeps pinned tasks in their partition even when the pointer crosses an unpinned row', async () => {
    api.list.mockResolvedValue({
      conversations: [row('first', 'owner/repo', true), row('second')],
      next_cursor: null,
    });
    render(<AgentsSidebar />);
    const first = (await screen.findByRole('link', { name: 'first' })).parentElement!;
    const second = screen.getByRole('link', { name: 'second' }).parentElement!;
    const dataTransfer = transfer();
    fireEvent.dragStart(first, { dataTransfer });
    over(second, 'dragOver', 125, dataTransfer);
    expect(second).not.toHaveAttribute('data-drop-position');
    over(second, 'drop', 125, dataTransfer);
    expect(taskOrder()).toEqual(['first', 'second']);
  });
  it('moves projects after a lower target using the same midpoint rule', async () => {
    api.projects.mockResolvedValue([
      { repo: 'owner/repo', task_count: 2, active_count: 0, last_activity_at: null },
      { repo: 'other/project', task_count: 0, active_count: 0, last_activity_at: null },
    ]);
    render(<AgentsSidebar />);
    const first = (await screen.findByRole('button', { name: 'repo' })).parentElement!;
    const second = screen.getByRole('button', { name: 'project' }).parentElement!;
    vi.spyOn(second, 'getBoundingClientRect').mockReturnValue({
      top: 100,
      height: 32,
      bottom: 132,
    } as DOMRect);
    const dataTransfer = transfer();
    fireEvent.pointerDown(first.querySelector('[data-drag-handle]')!);
    fireEvent.dragStart(first, { dataTransfer });
    over(second, 'dragOver', 125, dataTransfer);
    expect(second).toHaveAttribute('data-drop-position', 'after');
    over(second, 'drop', 125, dataTransfer);
    expect(JSON.parse(localStorage.getItem(sidebarPreferencesKey('drag-user'))!).projects).toEqual([
      'other/project',
      'owner/repo',
    ]);
    expect(screen.getByRole('link', { name: 'New task in project' })).toHaveAttribute(
      'draggable',
      'false',
    );
  });
});
