// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  clearMemories,
  getMemories,
  getMemoryProjects,
  setMemoryEnabled,
  type AgentMemoryScope,
} from '@/lib/api/agentMemory';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { MemorySettings } from './MemorySettings';

const auth = vi.hoisted(() => ({ userId: 'alice' as string | undefined }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.userId ? { id: auth.userId } : null } }),
}));
vi.mock('@/lib/api/agentMemory', () => ({
  getMemories: vi.fn(),
  getMemoryProjects: vi.fn(),
  addMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  restoreMemory: vi.fn(),
  clearMemories: vi.fn(),
  setMemoryEnabled: vi.fn(),
}));

const repo = 'alice/toolbox';

function scope(scopeRepo: string | null, content: string): AgentMemoryScope {
  return {
    repo: scopeRepo,
    enabled: true,
    memories: [
      {
        id: `m-${scopeRepo ?? 'user'}`,
        repo: scopeRepo,
        content,
        description: content,
        status: 'active',
        origin: 'manual',
        source_job_ids: [],
        version: 0,
        created_at: '2026-10-14T00:00:00Z',
        updated_at: '2026-10-14T00:00:00Z',
        last_used_at: null,
        archived_at: null,
      },
    ],
    archived: [],
  };
}

function card() {
  return (
    <LocaleProvider>
      <MemorySettings />
    </LocaleProvider>
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem(LOCALE_STORAGE_KEY, 'en');
  auth.userId = 'alice';
  vi.mocked(getMemoryProjects).mockResolvedValue([repo]);
  vi.mocked(getMemories).mockImplementation(async (selected) =>
    selected ? scope(selected, 'Use pnpm.') : scope(null, 'Reply in Chinese.'),
  );
  vi.mocked(setMemoryEnabled).mockImplementation(async (selected, enabled) => ({
    ...scope(selected, 'unused'),
    enabled,
  }));
});
afterEach(cleanup);

describe('memory settings card', () => {
  it('shows user memory first and switches scopes with the mouse or keyboard', async () => {
    render(card());
    expect(screen.getByRole('region', { name: 'Memory' })).toBeInTheDocument();
    const userTab = screen.getByRole('tab', { name: 'User memory' });
    const projectTab = screen.getByRole('tab', { name: 'Project memory' });
    expect(userTab).toHaveAttribute('aria-selected', 'true');
    await screen.findByText('Reply in Chinese.');

    fireEvent.click(projectTab);
    expect(projectTab).toHaveAttribute('aria-selected', 'true');
    const projectPanel = screen.getByRole('tabpanel', { name: 'Project memory' });
    expect(await within(projectPanel).findByText('Use pnpm.')).toBeVisible();

    fireEvent.keyDown(projectTab, { key: 'ArrowRight' });
    expect(userTab).toHaveFocus();
    expect(screen.getByRole('tabpanel', { name: 'User memory' })).toBeVisible();
  });

  it('keeps the user and project switches and clearing independent', async () => {
    render(card());
    const account = within(screen.getByRole('tabpanel', { name: 'User memory' }));
    await account.findByText('Reply in Chinese.');
    fireEvent.click(screen.getByRole('tab', { name: 'Project memory' }));
    const project = within(screen.getByRole('tabpanel', { name: 'Project memory' }));
    await project.findByText('Use pnpm.');

    vi.mocked(getMemories).mockResolvedValueOnce({ ...scope(repo, 'Use pnpm.'), enabled: false });
    fireEvent.click(project.getByRole('switch', { name: 'Use memory' }));
    await waitFor(() => expect(project.getByRole('switch')).not.toBeChecked());
    expect(setMemoryEnabled).toHaveBeenCalledWith(repo, false, expect.any(AbortSignal));
    expect(account.getByRole('switch', { name: 'Use memory', hidden: true })).toBeChecked();

    fireEvent.click(project.getByRole('button', { name: 'Clear' }));
    expect(
      project.getByRole('group', { name: `Clear the memory of ${repo}?` }),
    ).toBeInTheDocument();
    fireEvent.click(project.getByRole('button', { name: 'Yes, clear' }));
    await waitFor(() => expect(clearMemories).toHaveBeenCalledWith(repo, expect.any(AbortSignal)));
    expect(account.getByText('Reply in Chinese.')).toBeInTheDocument();
  });

  it('asks a signed-out visitor to sign in without loading memory', () => {
    auth.userId = undefined;
    render(card());
    expect(screen.getAllByText('Sign in to see your memory.')[0]).toBeInTheDocument();
    expect(getMemories).not.toHaveBeenCalled();
  });

  it('translates the card and tabs into Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(card());
    expect(screen.getByRole('region', { name: '记忆' })).toBeInTheDocument();
    expect(screen.getByRole('tablist', { name: '记忆范围' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: '项目记忆' }));
    expect(await screen.findByRole('combobox', { name: '仓库' })).toBeInTheDocument();
  });
});
