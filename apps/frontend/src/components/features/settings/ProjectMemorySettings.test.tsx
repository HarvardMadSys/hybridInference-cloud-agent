// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import {
  clearProjectMemory,
  consolidateProjectMemory,
  deleteProjectMemoryEntry,
  editProjectMemoryEntry,
  getMemoryProjects,
  getProjectMemory,
  saveProjectMemories,
  updateProjectMemory,
  type ProjectMemoryView,
} from '@/lib/api/projectMemory';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { APIError } from '@/lib/utils/errors';
import { ProjectMemorySettings } from './ProjectMemorySettings';

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

const repo = 'alice/toolbox';
const otherRepo = 'alice/other';
const agentConfig: AgentConfigApi = {
  repos: [],
  runtimes: ['codex'],
  models: ['model-a', 'model-b'],
  github_connected: false,
  github_install_url: null,
};
const empty: ProjectMemoryView = {
  enabled: true,
  revision: 1,
  updated_at: null,
  repo,
  portrait: {
    summary: '',
    projects: [],
    focus_areas: [],
    preferred_runtime: null,
    preferred_model: null,
  },
  memories: [],
  suggestions: [],
};
const populated: ProjectMemoryView = {
  ...empty,
  updated_at: '2026-10-11T12:00:00Z',
  portrait: {
    summary: 'Building reliable tools for this project.',
    projects: [repo],
    focus_areas: ['Testing'],
    preferred_runtime: 'codex',
    preferred_model: 'model-a',
  },
  memories: [
    {
      id: 'preference-1',
      kind: 'preference',
      content: 'Run the project test suite.',
      source_job_id: 'job/one',
      source_repo: repo,
      user_edited: false,
      scope: 'repo',
      created_at: '2026-10-11T12:00:00Z',
    },
    {
      id: 'task-1',
      kind: 'task',
      content: 'Fix the project test runner.',
      source_job_id: 'job_two',
      source_repo: repo,
      user_edited: false,
      scope: 'repo',
      created_at: null,
    },
  ],
  suggestions: [
    {
      id: 'suggestion-1',
      title: 'Include a validation command',
      reason: 'Project tasks often require test fixes.',
      action: 'Add the project test command to future requests.',
    },
  ],
};
const other: ProjectMemoryView = {
  ...empty,
  enabled: false,
  repo: otherRepo,
  portrait: { ...empty.portrait, summary: 'Work in the other repository.' },
};
function panel() {
  return (
    <LocaleProvider>
      <ProjectMemorySettings />
    </LocaleProvider>
  );
}

function openConsolidation() {
  fireEvent.click(screen.getByRole('button', { name: 'Consolidate' }));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((finish) => {
    resolve = finish;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  auth.userId = 'alice';
  vi.mocked(getAgentConfig).mockResolvedValue(agentConfig);
  vi.mocked(getMemoryProjects).mockResolvedValue([repo, otherRepo]);
  vi.mocked(getProjectMemory).mockImplementation(async (selected) =>
    selected === otherRepo ? other : populated,
  );
  vi.mocked(updateProjectMemory).mockResolvedValue({ ...populated, enabled: false });
  vi.mocked(clearProjectMemory).mockResolvedValue(empty);
});

afterEach(cleanup);

describe('project memory settings', () => {
  it('loads repository choices, the selected project’s profile, source-linked memories, and suggestions', async () => {
    render(panel());
    expect(screen.getByRole('status')).toHaveTextContent('Loading repositories');
    await screen.findByText(populated.portrait.summary);

    expect(getMemoryProjects).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(getProjectMemory).toHaveBeenCalledWith(repo, expect.any(AbortSignal));
    expect(screen.getByRole('combobox', { name: 'Repository' })).toHaveValue(repo);
    expect(
      within(screen.getByRole('combobox', { name: 'Repository' }))
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual([repo, otherRepo]);
    expect(screen.getByText(/takes priority over user memory/)).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Remember automatically' })).toBeChecked();
    fireEvent.click(screen.getByText('Profile and suggestions'));
    expect(screen.getByRole('heading', { name: 'Project profile' })).toBeVisible();
    expect(screen.getByText('Testing')).toBeInTheDocument();
    expect(screen.getByText('codex')).toBeInTheDocument();
    expect(screen.getByText('model-a', { selector: 'dd' })).toBeInTheDocument();
    openConsolidation();
    expect(await screen.findByRole('combobox', { name: 'Model' })).toHaveValue('model-a');
    expect(screen.getByText('Preference')).toBeInTheDocument();
    expect(screen.getByText('Task context')).toBeInTheDocument();
    expect(
      within(screen.getByRole('listitem', { name: 'Memory 1' })).getByText(repo),
    ).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Source task' })[0]).toHaveAttribute(
      'href',
      '/job%2Fone',
    );
    expect(screen.getAllByRole('link', { name: 'Source task' })[1]).toHaveAttribute(
      'href',
      '/job_two',
    );
    expect(screen.getByText(populated.suggestions[0].title)).toBeInTheDocument();
    expect(screen.getByText(populated.suggestions[0].reason)).toBeInTheDocument();
    expect(screen.getByText(populated.suggestions[0].action)).toBeInTheDocument();
    expect(screen.getByText(/^Updated /)).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('shows an empty repository list without fetching an arbitrary project', async () => {
    vi.mocked(getMemoryProjects).mockResolvedValue([]);
    render(panel());

    await screen.findByText('Project memory appears after you send a request in a repository.');
    expect(screen.getByRole('combobox', { name: 'Repository' })).toBeDisabled();
    expect(screen.getByRole('option', { name: 'Select a repository' })).toBeInTheDocument();
    expect(getProjectMemory).not.toHaveBeenCalled();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });

  it('shows project-specific empty states', async () => {
    vi.mocked(getProjectMemory).mockResolvedValue(empty);
    render(panel());

    await screen.findByText('No memories yet.');
    expect(screen.getByText('Appears after a few requests.')).toBeInTheDocument();
    expect(screen.getByText('None yet.')).toBeInTheDocument();
  });

  it('applies the canonical toggle response only when it arrives and refreshes the selected project', async () => {
    const mutation = deferred<ProjectMemoryView>();
    vi.mocked(updateProjectMemory).mockReturnValueOnce(mutation.promise);
    render(panel());
    const toggle = await screen.findByRole('switch');
    fireEvent.click(toggle);

    expect(updateProjectMemory).toHaveBeenCalledWith(repo, false, expect.any(AbortSignal));
    expect(toggle).toBeChecked();
    expect(toggle).toBeDisabled();
    const canonical = {
      ...populated,
      enabled: false,
      portrait: { ...populated.portrait, summary: 'Canonical server profile.' },
    };
    await act(async () => mutation.resolve(canonical));
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(canonical.portrait.summary)).toBeInTheDocument();

    vi.mocked(updateProjectMemory).mockResolvedValueOnce(populated);
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toBeChecked());
    expect(updateProjectMemory).toHaveBeenLastCalledWith(repo, true, expect.any(AbortSignal));

    vi.mocked(getProjectMemory).mockResolvedValueOnce(empty);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('No memories yet.');
    expect(getProjectMemory).toHaveBeenCalledTimes(2);
    expect(getProjectMemory).toHaveBeenLastCalledWith(repo, expect.any(AbortSignal));
  });

  it('retries repository-list errors and preserves the selected project while it remains available', async () => {
    vi.mocked(getMemoryProjects).mockRejectedValueOnce(new Error('Unavailable'));
    render(panel());
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load repositories');
    expect(getProjectMemory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh repositories' }));
    await screen.findByText(populated.portrait.summary);

    fireEvent.change(screen.getByRole('combobox', { name: 'Repository' }), {
      target: { value: otherRepo },
    });
    await screen.findByText(other.portrait.summary);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh repositories' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Repository' })).toBeEnabled());
    expect(screen.getByRole('combobox', { name: 'Repository' })).toHaveValue(otherRepo);
    expect(getProjectMemory).toHaveBeenCalledTimes(2);

    vi.mocked(getMemoryProjects).mockResolvedValueOnce([repo]);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh repositories' }));
    await screen.findByText(populated.portrait.summary);
    expect(screen.getByRole('combobox', { name: 'Repository' })).toHaveValue(repo);
    expect(getProjectMemory).toHaveBeenLastCalledWith(repo, expect.any(AbortSignal));
  });

  it('retries project load failures and preserves the server view after a mutation fails', async () => {
    vi.mocked(getProjectMemory).mockRejectedValueOnce(new Error('Unavailable'));
    render(panel());
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load or update memory');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    const toggle = await screen.findByRole('switch');

    vi.mocked(updateProjectMemory).mockRejectedValueOnce(new Error('Unavailable'));
    fireEvent.click(toggle);
    await screen.findByRole('alert');
    expect(toggle).toBeChecked();
    expect(toggle).toBeEnabled();
    expect(screen.getByText(populated.portrait.summary)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(getProjectMemory).toHaveBeenCalledTimes(3);
  });

  it('rejects a response for a repository other than the selected project', async () => {
    vi.mocked(getProjectMemory).mockResolvedValueOnce(other);
    render(panel());
    await screen.findByRole('alert');
    expect(screen.queryByText(other.portrait.summary)).not.toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText(populated.portrait.summary);
  });

  it('edits project content while paused, validates length, restores focus, and applies the canonical entry', async () => {
    const paused = { ...populated, enabled: false };
    const mutation = deferred<ProjectMemoryView>();
    vi.mocked(getProjectMemory).mockResolvedValueOnce(paused);
    vi.mocked(editProjectMemoryEntry).mockReturnValueOnce(mutation.promise);
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Memory 1' });
    const card = within(first);
    fireEvent.click(card.getByRole('button', { name: 'Edit' }));
    let editor = card.getByRole('textbox', { name: 'Memory content' });
    expect(editor).toHaveFocus();
    expect(editor).toHaveValue(populated.memories[0].content);
    expect(editor).toHaveAttribute('maxlength', '240');
    expect(card.getByRole('combobox', { name: 'Applies to' })).toHaveValue('repo');
    openConsolidation();
    expect(screen.getByRole('button', { name: 'Consolidate now' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Consolidate' }));
    fireEvent.change(editor, { target: { value: '   ' } });
    expect(card.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.change(editor, { target: { value: 'x'.repeat(241) } });
    expect(card.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.click(card.getByRole('button', { name: 'Cancel' }));
    expect(card.getByRole('button', { name: 'Edit' })).toHaveFocus();
    expect(editProjectMemoryEntry).not.toHaveBeenCalled();

    fireEvent.click(card.getByRole('button', { name: 'Edit' }));
    editor = card.getByRole('textbox');
    fireEvent.change(editor, { target: { value: 'Run focused project checks.' } });
    fireEvent.click(card.getByRole('button', { name: 'Save' }));
    expect(editProjectMemoryEntry).toHaveBeenCalledWith(
      repo,
      'preference-1',
      'Run focused project checks.',
      populated.revision,
      expect.any(AbortSignal),
    );
    expect(editor).toBeDisabled();
    expect(card.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    const saved = {
      ...paused,
      revision: 2,
      memories: [
        { ...paused.memories[0], content: 'Canonical project preference.', user_edited: true },
        paused.memories[1],
      ],
    };
    await act(async () => mutation.resolve(saved));
    expect(card.queryByRole('textbox')).not.toBeInTheDocument();
    expect(card.getByText('Canonical project preference.')).toBeInTheDocument();
    expect(card.getByText('Edited')).toBeInTheDocument();
    expect(card.getByRole('button', { name: 'Edit' })).toHaveFocus();
    expect(screen.getByRole('switch')).not.toBeChecked();
    expect(saveProjectMemories).not.toHaveBeenCalled();
  });

  it('preserves a project edit draft on conflict and retries with the refreshed revision only after Save', async () => {
    vi.mocked(editProjectMemoryEntry).mockRejectedValueOnce(
      new APIError('project_memory_conflict', 'private server detail', 409),
    );
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Memory 1' });
    const card = within(first);
    fireEvent.click(card.getByRole('button', { name: 'Edit' }));
    fireEvent.change(card.getByRole('textbox'), { target: { value: 'My project draft.' } });
    fireEvent.click(card.getByRole('button', { name: 'Save' }));
    expect(await card.findByRole('alert')).toHaveTextContent(
      'Refresh to review the latest saved text',
    );
    expect(card.getByRole('textbox')).toHaveValue('My project draft.');
    expect(screen.queryByText('private server detail')).not.toBeInTheDocument();
    const latest = {
      ...populated,
      revision: 7,
      memories: [
        { ...populated.memories[0], content: 'Project preference from another session.' },
        populated.memories[1],
      ],
    };
    vi.mocked(getProjectMemory).mockResolvedValueOnce(latest);
    fireEvent.click(card.getByRole('button', { name: 'Refresh memory' }));
    await card.findByText(latest.memories[0].content);
    expect(card.getByRole('textbox')).toHaveValue('My project draft.');
    expect(card.getByRole('textbox')).toHaveFocus();
    expect(editProjectMemoryEntry).toHaveBeenCalledTimes(1);
    vi.mocked(editProjectMemoryEntry).mockResolvedValueOnce({
      ...latest,
      revision: 8,
      memories: [{ ...latest.memories[0], content: 'My project draft.' }, latest.memories[1]],
    });
    fireEvent.click(card.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(card.queryByRole('textbox')).not.toBeInTheDocument());
    expect(editProjectMemoryEntry).toHaveBeenLastCalledWith(
      repo,
      'preference-1',
      'My project draft.',
      7,
      expect.any(AbortSignal),
    );
  });

  it.each([422, 503])(
    'keeps project drafts after a %s error without rendering server details',
    async (status) => {
      vi.mocked(editProjectMemoryEntry).mockRejectedValueOnce(
        new APIError('project_memory_error', 'credential-secret-do-not-render', status),
      );
      render(panel());
      const card = within(await screen.findByRole('listitem', { name: 'Memory 1' }));
      fireEvent.click(card.getByRole('button', { name: 'Edit' }));
      fireEvent.change(card.getByRole('textbox'), {
        target: { value: 'Keep this project draft.' },
      });
      fireEvent.click(card.getByRole('button', { name: 'Save' }));
      expect(await card.findByRole('alert')).toHaveTextContent(
        status === 422 ? 'leave out credentials' : 'Your draft is kept',
      );
      expect(card.getByRole('textbox')).toHaveValue('Keep this project draft.');
      expect(card.getByRole('button', { name: 'Save' })).toBeEnabled();
      expect(screen.queryByText('credential-secret-do-not-render')).not.toBeInTheDocument();
    },
  );

  it('requires entry removal confirmation, retains failed deletions, and focuses project memory after removal', async () => {
    vi.mocked(deleteProjectMemoryEntry)
      .mockRejectedValueOnce(new APIError('project_memory_error', 'private server detail', 503))
      .mockResolvedValueOnce({ ...populated, revision: 2, memories: [populated.memories[1]] });
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Memory 1' });
    const card = within(first);
    fireEvent.click(card.getByRole('button', { name: 'Remove' }));
    expect(card.getByRole('button', { name: 'Yes, remove' })).toHaveFocus();
    expect(deleteProjectMemoryEntry).not.toHaveBeenCalled();
    fireEvent.click(card.getByRole('button', { name: 'Cancel' }));
    expect(card.getByRole('button', { name: 'Remove' })).toHaveFocus();
    fireEvent.click(card.getByRole('button', { name: 'Remove' }));
    fireEvent.click(card.getByRole('button', { name: 'Yes, remove' }));
    await card.findByRole('alert');
    expect(card.getByText(populated.memories[0].content)).toBeInTheDocument();
    fireEvent.click(card.getByRole('button', { name: 'Yes, remove' }));
    await waitFor(() => expect(first).not.toBeInTheDocument());
    expect(deleteProjectMemoryEntry).toHaveBeenLastCalledWith(
      repo,
      'preference-1',
      populated.revision,
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('heading', { name: 'Saved memories' })).toHaveFocus();
    expect(screen.getByText(populated.memories[1].content)).toBeInTheDocument();
    expect(clearProjectMemory).not.toHaveBeenCalled();
  });

  it('consolidates the selected project’s revision and replaces entries with the returned profile and scopes', async () => {
    const consolidated = {
      ...populated,
      revision: 2,
      portrait: { ...populated.portrait, summary: 'Consolidated project profile.' },
      memories: [{ ...populated.memories[0], content: 'Keep project checks focused.' }],
    };
    vi.mocked(consolidateProjectMemory).mockResolvedValueOnce(consolidated);
    render(panel());
    await screen.findByRole('switch');
    openConsolidation();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Consolidate now' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Consolidate now' }));
    await screen.findByText('Keep project checks focused.');
    expect(consolidateProjectMemory).toHaveBeenCalledWith(
      repo,
      1,
      'model-a',
      expect.any(AbortSignal),
    );
    expect(screen.getByText(consolidated.portrait.summary)).toBeInTheDocument();
    expect(screen.queryByText(populated.memories[1].content)).not.toBeInTheDocument();
    expect(screen.getByText('1/12 · 28/2,400 characters')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Repository' })).toHaveValue(repo);
  });

  it('submits the chosen project model and keeps an explicit choice after memory refresh', async () => {
    vi.mocked(consolidateProjectMemory).mockResolvedValueOnce(populated);
    render(panel());
    await screen.findByRole('switch');
    openConsolidation();
    const selector = screen.getByRole('combobox', { name: 'Model' });
    await waitFor(() => expect(selector).toHaveValue('model-a'));
    expect(
      within(selector)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['model-a', 'model-b']);
    fireEvent.change(selector, { target: { value: 'model-b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Consolidate now' }));
    await waitFor(() =>
      expect(consolidateProjectMemory).toHaveBeenCalledWith(
        repo,
        1,
        'model-b',
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(screen.getByText(populated.memories[0].content)).toBeInTheDocument();
    expect(screen.getByText(populated.memories[1].content)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
    openConsolidation();
    expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('model-b');
  });

  it('uses the preferred project model when the catalog arrives after memory', async () => {
    const catalog = deferred<AgentConfigApi>();
    vi.mocked(getAgentConfig).mockReturnValueOnce(catalog.promise);
    render(panel());
    await screen.findByRole('switch');
    openConsolidation();
    const selector = screen.getByRole('combobox', { name: 'Model' });
    expect(selector).toBeDisabled();
    await act(async () => catalog.resolve({ ...agentConfig, models: ['model-b', 'model-a'] }));
    expect(selector).toHaveValue('model-a');
  });

  it('defaults to the first available project model when the preferred model is unavailable', async () => {
    vi.mocked(getAgentConfig).mockResolvedValueOnce({
      ...agentConfig,
      models: ['model-b', 'model-c'],
    });
    render(panel());
    await screen.findByRole('switch');
    openConsolidation();
    const selector = screen.getByRole('combobox', { name: 'Model' });
    await waitFor(() => expect(selector).toHaveValue('model-b'));
  });

  it('keeps project controls usable while models load and retries an empty catalog without reloading memory', async () => {
    const catalog = deferred<AgentConfigApi>();
    vi.mocked(getAgentConfig).mockReturnValueOnce(catalog.promise);
    render(panel());
    await screen.findByRole('switch');
    openConsolidation();
    const selector = screen.getByRole('combobox', { name: 'Model' });
    expect(selector).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Consolidate now' })).toBeDisabled();
    expect(within(selector).getByRole('option')).toHaveTextContent('Loading models');
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    expect(screen.getByRole('switch')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Clear' })).toBeEnabled();
    await act(async () => catalog.resolve({ ...agentConfig, models: [] }));
    expect(within(selector).getByRole('option')).toHaveTextContent('No models available');
    expect(selector).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Consolidate now' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    expect(consolidateProjectMemory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Reload models' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Consolidate now' })).toBeEnabled(),
    );
    expect(selector).toHaveValue('model-a');
    expect(selector).toBeEnabled();
    expect(getAgentConfig).toHaveBeenCalledTimes(2);
    expect(getProjectMemory).toHaveBeenCalledTimes(1);
  });

  it('disables project consolidation after a catalog error and retries without reloading memory', async () => {
    vi.mocked(getAgentConfig).mockRejectedValueOnce(new Error('Offline'));
    render(panel());
    await screen.findByRole('switch');
    openConsolidation();
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load models');
    expect(screen.getByRole('button', { name: 'Consolidate now' })).toBeDisabled();
    expect(screen.getByRole('switch')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Clear' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Reload models' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Consolidate now' })).toBeEnabled(),
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(getAgentConfig).toHaveBeenCalledTimes(2);
    expect(getProjectMemory).toHaveBeenCalledTimes(1);
  });

  it.each(['repository', 'account'] as const)(
    'discards stale project catalogs and resets model choices when the %s changes',
    async (identity) => {
      const oldCatalog = deferred<AgentConfigApi>();
      const newCatalog = deferred<AgentConfigApi>();
      vi.mocked(getAgentConfig)
        .mockReturnValueOnce(oldCatalog.promise)
        .mockReturnValueOnce(newCatalog.promise);
      const rendered = render(panel());
      await screen.findByRole('switch');
      openConsolidation();
      if (identity === 'account') {
        auth.userId = 'bob';
        rendered.rerender(panel());
        await screen.findByText(populated.portrait.summary);
      } else {
        // Consolidation needs saved memories, so the next project has some.
        vi.mocked(getProjectMemory).mockResolvedValueOnce({
          ...populated,
          repo: otherRepo,
          portrait: other.portrait,
        });
        fireEvent.change(screen.getByRole('combobox', { name: 'Repository' }), {
          target: { value: otherRepo },
        });
        await screen.findByText(other.portrait.summary);
      }
      expect(screen.queryByRole('combobox', { name: 'Model' })).not.toBeInTheDocument();
      openConsolidation();
      await waitFor(() => expect(getAgentConfig).toHaveBeenCalledTimes(2));
      await act(async () => newCatalog.resolve({ ...agentConfig, models: ['new-owner-model'] }));
      const selector = screen.getByRole('combobox', { name: 'Model' });
      expect(selector).toHaveValue('new-owner-model');
      await act(async () => oldCatalog.resolve({ ...agentConfig, models: ['stale-owner-model'] }));
      expect(selector).toHaveValue('new-owner-model');
      expect(screen.queryByRole('option', { name: 'stale-owner-model' })).not.toBeInTheDocument();
    },
  );

  it.each(['repository', 'account'] as const)(
    'resets project model selection and aborts consolidation when the %s changes',
    async (identity) => {
      const consolidation = deferred<ProjectMemoryView>();
      vi.mocked(consolidateProjectMemory).mockReturnValueOnce(consolidation.promise);
      const rendered = render(panel());
      await screen.findByRole('switch');
      openConsolidation();
      const selector = screen.getByRole('combobox', { name: 'Model' });
      await waitFor(() => expect(selector).toBeEnabled());
      fireEvent.change(selector, { target: { value: 'model-b' } });
      fireEvent.click(screen.getByRole('button', { name: 'Consolidate now' }));
      const oldSignal = vi.mocked(consolidateProjectMemory).mock.calls[0][3]!;
      expect(consolidateProjectMemory).toHaveBeenCalledWith(
        repo,
        1,
        'model-b',
        expect.any(AbortSignal),
      );
      const selectedRepo = identity === 'account' ? repo : otherRepo;
      const next = {
        ...populated,
        repo: selectedRepo,
        portrait: { ...populated.portrait, preferred_model: 'new-owner-model' },
        memories: [
          { ...populated.memories[0], content: 'New project memory.', source_repo: selectedRepo },
        ],
      };
      vi.mocked(getProjectMemory).mockResolvedValueOnce(next);
      vi.mocked(getAgentConfig).mockResolvedValueOnce({
        ...agentConfig,
        models: ['new-owner-model'],
      });
      if (identity === 'account') {
        auth.userId = 'bob';
        rendered.rerender(panel());
      } else {
        fireEvent.change(screen.getByRole('combobox', { name: 'Repository' }), {
          target: { value: otherRepo },
        });
      }
      expect(oldSignal.aborted).toBe(true);
      expect(screen.queryByRole('group', { name: 'Consolidate memory' })).not.toBeInTheDocument();
      await screen.findByText('New project memory.');
      openConsolidation();
      const nextSelector = screen.getByRole('combobox', { name: 'Model' });
      await waitFor(() => expect(nextSelector).toHaveValue('new-owner-model'));
      await act(async () => consolidation.resolve(empty));
      expect(screen.getByText('New project memory.')).toBeInTheDocument();
      expect(nextSelector).toHaveValue('new-owner-model');
    },
  );

  it('promotes a project preference to user scope with a revision-checked list correction', async () => {
    vi.mocked(saveProjectMemories).mockResolvedValueOnce({
      ...populated,
      revision: 2,
      memories: [populated.memories[1]],
    });
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Memory 1' });
    const card = within(first);
    fireEvent.click(card.getByRole('button', { name: 'Edit' }));
    fireEvent.change(card.getByRole('combobox', { name: 'Applies to' }), {
      target: { value: 'user' },
    });
    openConsolidation();
    expect(screen.getByRole('button', { name: 'Consolidate now' })).toBeDisabled();
    fireEvent.click(card.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(saveProjectMemories).toHaveBeenCalledWith(
        repo,
        1,
        [
          { id: 'preference-1', content: populated.memories[0].content, scope: 'user' },
          { id: 'task-1', content: populated.memories[1].content, scope: 'repo' },
        ],
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() => expect(first).not.toBeInTheDocument());
    expect(screen.getByText(populated.memories[1].content)).toBeInTheDocument();
    expect(screen.queryByText(populated.memories[0].content)).not.toBeInTheDocument();
    expect(editProjectMemoryEntry).not.toHaveBeenCalled();
  });

  it('retains an unavailable project draft for copying after refresh and discards it on repository change', async () => {
    vi.mocked(editProjectMemoryEntry).mockRejectedValueOnce(
      new APIError('project_memory_missing', 'private source detail', 404),
    );
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Memory 1' });
    const card = within(first);
    fireEvent.click(card.getByRole('button', { name: 'Edit' }));
    fireEvent.change(card.getByRole('textbox'), { target: { value: 'Copy this project draft.' } });
    fireEvent.click(card.getByRole('button', { name: 'Save' }));
    await card.findByRole('alert');
    vi.mocked(getProjectMemory).mockResolvedValueOnce({ ...populated, revision: 3, memories: [] });
    fireEvent.click(card.getByRole('button', { name: 'Refresh memory' }));
    expect(await card.findByRole('alert')).toHaveTextContent('Your draft is kept for copying');
    const editor = card.getByRole('textbox') as HTMLTextAreaElement;
    expect(editor).toHaveValue('Copy this project draft.');
    expect(editor).toHaveAttribute('readonly');
    expect(editor).toBeEnabled();
    expect(card.getByRole('button', { name: 'Save' })).toBeDisabled();
    editor.select();
    expect(editor.selectionEnd).toBe('Copy this project draft.'.length);
    fireEvent.change(screen.getByRole('combobox', { name: 'Repository' }), {
      target: { value: otherRepo },
    });
    await screen.findByText(other.portrait.summary);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByText(populated.memories[0].content)).not.toBeInTheDocument();
  });

  it.each(['repository', 'account'] as const)(
    'aborts project entry edits and discards drafts when the %s changes',
    async (identity) => {
      const mutation = deferred<ProjectMemoryView>();
      vi.mocked(editProjectMemoryEntry).mockReturnValueOnce(mutation.promise);
      const rendered = render(panel());
      const card = within(await screen.findByRole('listitem', { name: 'Memory 1' }));
      fireEvent.click(card.getByRole('button', { name: 'Edit' }));
      fireEvent.change(card.getByRole('textbox'), { target: { value: 'Private project draft.' } });
      fireEvent.click(card.getByRole('button', { name: 'Save' }));
      const oldSignal = vi.mocked(editProjectMemoryEntry).mock.calls[0][4]!;
      const next = {
        ...other,
        repo: identity === 'account' ? repo : otherRepo,
        portrait: { ...empty.portrait, summary: 'Fresh private project context.' },
      };
      vi.mocked(getProjectMemory).mockResolvedValueOnce(next);
      if (identity === 'account') {
        auth.userId = 'bob';
        rendered.rerender(panel());
      } else {
        fireEvent.change(screen.getByRole('combobox', { name: 'Repository' }), {
          target: { value: otherRepo },
        });
      }
      expect(oldSignal.aborted).toBe(true);
      expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
      await screen.findByText(next.portrait.summary);
      await act(async () => mutation.resolve(populated));
      expect(screen.getByText(next.portrait.summary)).toBeInTheDocument();
      expect(screen.getByRole('switch')).not.toBeChecked();
      expect(screen.queryByText(populated.memories[0].content)).not.toBeInTheDocument();
    },
  );

  it('aborts and ignores an old fetch when the repository changes', async () => {
    const oldRead = deferred<ProjectMemoryView>();
    const newRead = deferred<ProjectMemoryView>();
    vi.mocked(getProjectMemory)
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(newRead.promise);
    render(panel());
    await waitFor(() => expect(getProjectMemory).toHaveBeenCalledTimes(1));
    const oldSignal = vi.mocked(getProjectMemory).mock.calls[0][1]!;

    fireEvent.change(screen.getByRole('combobox', { name: 'Repository' }), {
      target: { value: otherRepo },
    });
    expect(oldSignal.aborted).toBe(true);
    await act(async () => oldRead.resolve(populated));
    expect(screen.queryByText(populated.portrait.summary)).not.toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    await act(async () => newRead.resolve(other));
    expect(screen.getByText(other.portrait.summary)).toBeInTheDocument();
    expect(screen.getByRole('switch')).not.toBeChecked();
  });

  it.each(['toggle', 'clear'] as const)(
    'aborts and ignores an old %s when the repository changes',
    async (operation) => {
      const mutation = deferred<ProjectMemoryView>();
      if (operation === 'toggle')
        vi.mocked(updateProjectMemory).mockReturnValueOnce(mutation.promise);
      else vi.mocked(clearProjectMemory).mockReturnValueOnce(mutation.promise);
      render(panel());
      await screen.findByText(populated.portrait.summary);
      if (operation === 'toggle') fireEvent.click(screen.getByRole('switch'));
      else {
        fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
        fireEvent.click(screen.getByRole('button', { name: 'Yes, clear' }));
      }
      const oldSignal =
        operation === 'toggle'
          ? vi.mocked(updateProjectMemory).mock.calls[0][2]!
          : vi.mocked(clearProjectMemory).mock.calls[0][1]!;

      fireEvent.change(screen.getByRole('combobox', { name: 'Repository' }), {
        target: { value: otherRepo },
      });
      expect(screen.queryByText(populated.portrait.summary)).not.toBeInTheDocument();
      expect(oldSignal.aborted).toBe(true);
      await screen.findByText(other.portrait.summary);
      await act(async () => mutation.resolve(populated));
      expect(screen.getByRole('switch')).not.toBeChecked();
      expect(screen.getByText(other.portrait.summary)).toBeInTheDocument();
      expect(screen.queryByText(populated.memories[0].content)).not.toBeInTheDocument();
      expect(screen.queryByRole('group', { name: /^Clear memory for/ })).not.toBeInTheDocument();
    },
  );

  it.each(['read', 'toggle', 'clear'] as const)(
    'aborts and ignores an old %s after changing accounts even for the same repository',
    async (operation) => {
      const oldRequest = deferred<ProjectMemoryView>();
      const newRead = deferred<ProjectMemoryView>();
      if (operation === 'read') vi.mocked(getProjectMemory).mockReturnValueOnce(oldRequest.promise);
      else if (operation === 'toggle')
        vi.mocked(updateProjectMemory).mockReturnValueOnce(oldRequest.promise);
      else vi.mocked(clearProjectMemory).mockReturnValueOnce(oldRequest.promise);
      const rendered = render(panel());
      await waitFor(() => expect(getProjectMemory).toHaveBeenCalledTimes(1));
      if (operation !== 'read') {
        await screen.findByText(populated.portrait.summary);
        if (operation === 'toggle') fireEvent.click(screen.getByRole('switch'));
        else {
          fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
          fireEvent.click(screen.getByRole('button', { name: 'Yes, clear' }));
        }
      }
      const oldSignal =
        operation === 'read'
          ? vi.mocked(getProjectMemory).mock.calls[0][1]!
          : operation === 'toggle'
            ? vi.mocked(updateProjectMemory).mock.calls[0][2]!
            : vi.mocked(clearProjectMemory).mock.calls[0][1]!;
      vi.mocked(getProjectMemory).mockReturnValueOnce(newRead.promise);
      auth.userId = 'bob';
      rendered.rerender(panel());
      expect(screen.queryByText(populated.portrait.summary)).not.toBeInTheDocument();
      expect(screen.queryByRole('group', { name: /^Clear memory for/ })).not.toBeInTheDocument();
      expect(oldSignal.aborted).toBe(true);
      await waitFor(() => expect(getProjectMemory).toHaveBeenCalledTimes(2));
      const bob = {
        ...empty,
        enabled: false,
        portrait: { ...empty.portrait, summary: 'Bob’s private project context.' },
      };
      await act(async () => newRead.resolve(bob));
      await act(async () => oldRequest.resolve(populated));
      expect(screen.getByText(bob.portrait.summary)).toBeInTheDocument();
      expect(screen.getByRole('switch')).not.toBeChecked();
      expect(screen.queryByText(populated.memories[0].content)).not.toBeInTheDocument();
    },
  );

  it('aborts and ignores the previous account’s repository list', async () => {
    const oldList = deferred<string[]>();
    const newList = deferred<string[]>();
    vi.mocked(getMemoryProjects)
      .mockReturnValueOnce(oldList.promise)
      .mockReturnValueOnce(newList.promise);
    const rendered = render(panel());
    const oldSignal = vi.mocked(getMemoryProjects).mock.calls[0][0]!;
    auth.userId = 'bob';
    rendered.rerender(panel());
    expect(oldSignal.aborted).toBe(true);
    await act(async () => newList.resolve([otherRepo]));
    await screen.findByText(other.portrait.summary);
    await act(async () => oldList.resolve([repo]));

    expect(screen.getByRole('combobox', { name: 'Repository' })).toHaveValue(otherRepo);
    expect(screen.queryByRole('option', { name: repo })).not.toBeInTheDocument();
    expect(getProjectMemory).toHaveBeenCalledTimes(1);
    expect(getAgentConfig).not.toHaveBeenCalled();
    expect(getProjectMemory).toHaveBeenCalledWith(otherRepo, expect.any(AbortSignal));
  });

  it('aborts the list and project read on unmount and does not fetch while signed out', async () => {
    const list = deferred<string[]>();
    vi.mocked(getMemoryProjects).mockReturnValueOnce(list.promise);
    const first = render(panel());
    const listSignal = vi.mocked(getMemoryProjects).mock.calls[0][0]!;
    first.unmount();
    expect(listSignal.aborted).toBe(true);
    await act(async () => list.resolve([repo]));
    expect(getProjectMemory).not.toHaveBeenCalled();

    const read = deferred<ProjectMemoryView>();
    vi.mocked(getProjectMemory).mockReturnValueOnce(read.promise);
    const second = render(panel());
    await waitFor(() => expect(getProjectMemory).toHaveBeenCalledTimes(1));
    const readSignal = vi.mocked(getProjectMemory).mock.calls[0][1]!;
    second.unmount();
    expect(readSignal.aborted).toBe(true);
    await act(async () => read.resolve(populated));

    auth.userId = undefined;
    render(panel());
    expect(screen.getByText('Sign in to see your memory.')).toBeInTheDocument();
    expect(getMemoryProjects).toHaveBeenCalledTimes(2);
    expect(getProjectMemory).toHaveBeenCalledTimes(1);
    expect(getAgentConfig).not.toHaveBeenCalled();
  });

  it('translates project choice, memory controls, and the scoped confirmation into Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(panel());
    expect(await screen.findByRole('switch', { name: '自动记忆' })).toBeChecked();
    expect(screen.getByText('仅用于该仓库，与用户记忆冲突时优先。')).toBeInTheDocument();
    fireEvent.click(screen.getByText('画像与建议'));
    expect(screen.getByRole('heading', { name: '项目画像' })).toBeVisible();
    expect(screen.getByRole('combobox', { name: '仓库' })).toHaveValue(repo);
    expect(screen.getByRole('button', { name: '刷新仓库列表' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '整合' }));
    expect(await screen.findByRole('combobox', { name: '模型' })).toHaveValue('model-a');
    expect(screen.getByRole('button', { name: '立即整合' })).toBeEnabled();
    const card = within(screen.getByRole('listitem', { name: '记忆 1' }));
    fireEvent.click(card.getByRole('button', { name: '编辑' }));
    expect(card.getByRole('textbox', { name: '记忆内容' })).toHaveFocus();
    expect(card.getByRole('combobox', { name: '适用范围' })).toHaveValue('repo');
    expect(card.getByRole('button', { name: '保存' })).toBeInTheDocument();
    fireEvent.click(card.getByRole('button', { name: '取消' }));
    fireEvent.click(screen.getByRole('button', { name: '清除' }));
    expect(screen.getByRole('group', { name: `清除 ${repo} 的记忆？` })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认清除' })).toHaveFocus();
    expect(screen.getByText(/用户记忆和其他仓库不受影响/)).toBeInTheDocument();
  });
});
