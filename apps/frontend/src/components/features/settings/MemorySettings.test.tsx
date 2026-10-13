// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { getAgentConfig } from '@/lib/api/agents';
import {
  clearProjectMemory,
  getMemoryProjects,
  getProjectMemory,
  updateProjectMemory,
  type ProjectMemoryView,
} from '@/lib/api/projectMemory';
import {
  clearUserMemory,
  getUserMemory,
  updateUserMemory,
  type UserMemoryView,
} from '@/lib/api/userMemory';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { MemorySettings } from './MemorySettings';

const auth = vi.hoisted(() => ({ userId: 'alice' as string | undefined }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.userId ? { id: auth.userId } : null } }),
}));
vi.mock('@/lib/api/agents', () => ({ getAgentConfig: vi.fn() }));
vi.mock('@/lib/api/projectMemory', () => ({
  getMemoryProjects: vi.fn(),
  getProjectMemory: vi.fn(),
  updateProjectMemory: vi.fn(),
  clearProjectMemory: vi.fn(),
  editProjectMemoryEntry: vi.fn(),
  deleteProjectMemoryEntry: vi.fn(),
  saveProjectMemories: vi.fn(),
  consolidateProjectMemory: vi.fn(),
}));
vi.mock('@/lib/api/userMemory', () => ({
  getUserMemory: vi.fn(),
  updateUserMemory: vi.fn(),
  clearUserMemory: vi.fn(),
  consolidateUserMemory: vi.fn(),
  saveUserMemories: vi.fn(),
  editUserMemoryEntry: vi.fn(),
  deleteUserMemoryEntry: vi.fn(),
}));

const repo = 'alice/toolbox';
const otherRepo = 'alice/other';
const portrait = {
  summary: '',
  projects: [],
  focus_areas: [],
  preferred_runtime: null,
  preferred_model: null,
};
const project: ProjectMemoryView = {
  enabled: true,
  revision: 1,
  updated_at: null,
  repo,
  portrait: { ...portrait, summary: 'Building reliable tools for this project.' },
  memories: [
    {
      id: 'project-preference',
      kind: 'preference',
      content: 'Run the project test suite.',
      source_job_id: 'job/one',
      source_repo: repo,
      user_edited: false,
      scope: 'repo',
      created_at: null,
    },
  ],
  suggestions: [],
};
const other: ProjectMemoryView = {
  ...project,
  enabled: false,
  repo: otherRepo,
  portrait: { ...portrait, summary: 'Work in the other repository.' },
  memories: [],
};
const user: UserMemoryView = {
  enabled: true,
  revision: 1,
  updated_at: null,
  avatar: { style: 'illustrated', data_url: 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=' },
  portrait: { ...portrait, summary: 'Account-wide preferences.' },
  memories: [
    {
      id: 'user-preference',
      kind: 'preference',
      content: 'Use small pull requests across projects.',
      source_job_id: 'user-job',
      source_repo: 'alice/user-source',
      user_edited: false,
      scope: 'user',
      created_at: null,
    },
  ],
  suggestions: [],
};

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
  auth.userId = 'alice';
  vi.mocked(getAgentConfig).mockResolvedValue({
    repos: [],
    runtimes: ['codex'],
    models: ['model-a'],
    github_connected: false,
    github_install_url: null,
  });
  vi.mocked(getMemoryProjects).mockResolvedValue([repo, otherRepo]);
  vi.mocked(getProjectMemory).mockImplementation(async (selected) =>
    selected === otherRepo ? other : project,
  );
  vi.mocked(updateProjectMemory).mockResolvedValue({ ...project, enabled: false });
  vi.mocked(clearProjectMemory).mockResolvedValue({ ...project, enabled: false, memories: [] });
  vi.mocked(getUserMemory).mockResolvedValue(user);
  vi.mocked(updateUserMemory).mockResolvedValue({ ...user, enabled: false });
  vi.mocked(clearUserMemory).mockResolvedValue({ ...user, portrait, memories: [] });
});

afterEach(cleanup);

describe('memory settings card', () => {
  it('shows user memory first and switches scopes with the mouse or keyboard', async () => {
    render(card());
    expect(screen.getByRole('heading', { name: 'Memory' })).toBeInTheDocument();
    const userTab = screen.getByRole('tab', { name: 'User memory' });
    const projectTab = screen.getByRole('tab', { name: 'Project memory' });
    expect(userTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel', { name: 'User memory' })).toBeVisible();
    expect(screen.queryByRole('tabpanel', { name: 'Project memory' })).not.toBeInTheDocument();
    // Both scopes load up front, so a switch shows memory without waiting.
    await screen.findByText(user.memories[0].content);
    await waitFor(() =>
      expect(getProjectMemory).toHaveBeenCalledWith(repo, expect.any(AbortSignal)),
    );

    fireEvent.click(projectTab);
    expect(projectTab).toHaveAttribute('aria-selected', 'true');
    expect(projectTab).toHaveAttribute('tabindex', '0');
    expect(userTab).toHaveAttribute('tabindex', '-1');
    const projectPanel = screen.getByRole('tabpanel', { name: 'Project memory' });
    expect(await within(projectPanel).findByText(project.memories[0].content)).toBeVisible();

    fireEvent.keyDown(projectTab, { key: 'ArrowRight' });
    expect(userTab).toHaveFocus();
    expect(userTab).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(userTab, { key: 'End' });
    expect(projectTab).toHaveFocus();
    fireEvent.keyDown(projectTab, { key: 'Home' });
    expect(userTab).toHaveFocus();
    expect(screen.getByRole('tabpanel', { name: 'User memory' })).toBeVisible();
  });

  it('keeps an open draft when switching tabs', async () => {
    render(card());
    const entry = await screen.findByRole('listitem', { name: 'Memory 1' });
    fireEvent.click(within(entry).getByRole('button', { name: 'Edit' }));
    fireEvent.change(within(entry).getByRole('textbox'), {
      target: { value: 'Draft in progress' },
    });
    fireEvent.click(screen.getByRole('tab', { name: 'Project memory' }));
    fireEvent.click(screen.getByRole('tab', { name: 'User memory' }));
    expect(within(entry).getByRole('textbox')).toHaveValue('Draft in progress');
  });

  it('keeps user and project toggles and clearing independent, with a scoped confirmation and restored focus', async () => {
    render(card());
    const userPanel = screen.getByRole('tabpanel', { name: 'User memory' });
    const account = within(userPanel);
    const userToggle = await account.findByRole('switch');
    fireEvent.click(screen.getByRole('tab', { name: 'Project memory' }));
    const projectPanel = within(screen.getByRole('tabpanel', { name: 'Project memory' }));
    const projectToggle = await projectPanel.findByRole('switch');
    expect(account.queryByRole('img', { hidden: true })).not.toBeInTheDocument();

    fireEvent.click(projectToggle);
    await waitFor(() => expect(projectToggle).not.toBeChecked());
    expect(userToggle).toBeChecked();
    expect(updateUserMemory).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('tab', { name: 'User memory' }));
    fireEvent.click(userToggle);
    await waitFor(() => expect(userToggle).not.toBeChecked());
    expect(updateProjectMemory).toHaveBeenCalledTimes(1);
    expect(projectToggle).not.toBeChecked();

    fireEvent.click(screen.getByRole('tab', { name: 'Project memory' }));
    const clearButton = projectPanel.getByRole('button', { name: 'Clear' });
    fireEvent.click(clearButton);
    expect(
      projectPanel.getByRole('group', { name: `Clear memory for ${repo}?` }),
    ).toBeInTheDocument();
    expect(
      projectPanel.getByText(/User memory and other repositories are not affected/),
    ).toBeInTheDocument();
    expect(projectPanel.getByRole('button', { name: 'Yes, clear' })).toHaveFocus();
    expect(clearProjectMemory).not.toHaveBeenCalled();
    fireEvent.click(projectPanel.getByRole('button', { name: 'Cancel' }));
    expect(clearButton).toHaveFocus();
    expect(clearProjectMemory).not.toHaveBeenCalled();

    fireEvent.click(clearButton);
    fireEvent.click(projectPanel.getByRole('button', { name: 'Yes, clear' }));
    await projectPanel.findByText('No memories yet.');
    expect(clearProjectMemory).toHaveBeenCalledWith(repo, expect.any(AbortSignal));
    expect(clearButton).toHaveFocus();
    expect(
      projectPanel.queryByRole('group', { name: `Clear memory for ${repo}?` }),
    ).not.toBeInTheDocument();
    expect(projectToggle).not.toBeChecked();
    expect(account.getByText(user.memories[0].content)).toBeInTheDocument();
    expect(clearUserMemory).not.toHaveBeenCalled();

    fireEvent.change(projectPanel.getByRole('combobox', { name: 'Repository' }), {
      target: { value: otherRepo },
    });
    await projectPanel.findByText(other.portrait.summary);
    fireEvent.click(screen.getByRole('tab', { name: 'User memory' }));
    fireEvent.click(account.getByRole('button', { name: 'Clear' }));
    expect(account.getByText(/Project memory is not affected/)).toBeInTheDocument();
    fireEvent.click(account.getByRole('button', { name: 'Yes, clear' }));
    await account.findByText('No memories yet.');
    expect(projectPanel.getByText(other.portrait.summary)).toBeInTheDocument();
    expect(clearProjectMemory).toHaveBeenCalledTimes(1);
  });

  it('translates the card and tabs into Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(card());
    expect(screen.getByRole('heading', { name: '记忆' })).toBeInTheDocument();
    expect(screen.getByRole('tablist', { name: '记忆范围' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '用户记忆' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(screen.getByRole('tab', { name: '项目记忆' }));
    expect(screen.getByRole('tabpanel', { name: '项目记忆' })).toBeVisible();
    expect(await screen.findByRole('combobox', { name: '仓库' })).toBeInTheDocument();
  });
});
