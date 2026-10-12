// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import {
  clearUserMemory,
  consolidateUserMemory,
  saveUserMemories,
  deleteUserMemoryEntry,
  editUserMemoryEntry,
  getUserMemory,
  updateUserMemory,
  type UserMemoryView,
} from '@/lib/api/userMemory';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { APIError } from '@/lib/utils/errors';
import { UserMemorySettings } from './UserMemorySettings';

const auth = vi.hoisted(() => ({ userId: 'alice' as string | undefined }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.userId ? { id: auth.userId } : null } }),
}));
vi.mock('@/lib/api/userMemory', () => ({
  consolidateUserMemory: vi.fn(),
  saveUserMemories: vi.fn(),
  getUserMemory: vi.fn(),
  updateUserMemory: vi.fn(),
  clearUserMemory: vi.fn(),
  editUserMemoryEntry: vi.fn(),
  deleteUserMemoryEntry: vi.fn(),
}));
vi.mock('@/lib/api/agents', () => ({ getAgentConfig: vi.fn() }));

const agentConfig: AgentConfigApi = {
  repos: [],
  runtimes: ['codex'],
  models: ['model-a', 'model-b'],
  github_connected: false,
  github_install_url: null,
};

function illustratedAvatar(color: string): UserMemoryView['avatar'] {
  return {
    style: 'illustrated',
    data_url: `data:image/svg+xml,${encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg"><rect width="80" height="80" fill="${color}"/></svg>`,
    )}`,
  };
}

const empty: UserMemoryView = {
  enabled: true,
  revision: 1,
  updated_at: null,
  avatar: illustratedAvatar('#98a3af'),
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

const populated: UserMemoryView = {
  ...empty,
  avatar: illustratedAvatar('#527eb5'),
  updated_at: '2026-10-11T12:00:00Z',
  portrait: {
    summary: 'Working on useful developer tools.',
    projects: ['alice/toolbox'],
    focus_areas: ['Testing'],
    preferred_runtime: 'codex',
    preferred_model: 'model-a',
  },
  memories: [
    {
      id: 'preference-1',
      kind: 'preference',
      content: 'Use small pull requests.',
      source_job_id: 'job/one',
      source_repo: 'alice/toolbox',
      user_edited: false,
      created_at: '2026-10-11T12:00:00Z',
    },
    {
      id: 'task-1',
      kind: 'task',
      content: 'Fix the test runner.',
      source_job_id: 'job_two',
      source_repo: null,
      user_edited: false,
      created_at: null,
    },
  ],
  suggestions: [
    {
      id: 'suggestion-1',
      title: 'Include a validation command',
      reason: 'Recent tasks often require test fixes.',
      action: 'Add the expected test command to future requests.',
    },
  ],
};

function panel() {
  return (
    <LocaleProvider>
      <UserMemorySettings />
    </LocaleProvider>
  );
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
  vi.mocked(getUserMemory).mockResolvedValue(populated);
  vi.mocked(updateUserMemory).mockResolvedValue({ ...populated, enabled: false });
  vi.mocked(clearUserMemory).mockResolvedValue(empty);
});

afterEach(cleanup);

describe('memory settings', () => {
  it('automatically loads recent work, source-linked memory, and actionable suggestions without an avatar', async () => {
    render(panel());
    expect(screen.getByRole('status')).toHaveTextContent('Loading your memory');
    await screen.findByRole('switch');
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'User memory' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Saved user memory' })).toBeInTheDocument();
    expect(screen.getByText(/User preferences apply to all projects/)).toHaveTextContent(
      'preferences explicitly marked “across all projects”',
    );
    expect(screen.getByRole('switch', { name: 'Automatically build memory' })).toBeChecked();
    expect(getUserMemory).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(screen.getByText(populated.portrait.summary)).toBeInTheDocument();
    expect(screen.getByText('alice/toolbox')).toBeInTheDocument();
    expect(screen.getByText('Testing')).toBeInTheDocument();
    expect(screen.getByText('codex')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Consolidation model' })).toHaveValue('model-a');
    expect(screen.getByText('Preference')).toBeInTheDocument();
    expect(screen.getByText('Task context')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'View source task' })[0]).toHaveAttribute(
      'href',
      '/job%2Fone',
    );
    expect(screen.getByText(populated.suggestions[0].title)).toBeInTheDocument();
    expect(screen.getByText(populated.suggestions[0].reason)).toBeInTheDocument();
    expect(screen.getByText(populated.suggestions[0].action)).toBeInTheDocument();
    expect(screen.getByText(/Memory updated/)).toBeInTheDocument();
  });

  it('renders useful empty states', async () => {
    vi.mocked(getUserMemory).mockResolvedValue(empty);
    render(panel());
    await screen.findByRole('switch');
    expect(screen.getByText(/Recent work will appear/)).toBeInTheDocument();
    expect(screen.getByText('No saved memory yet.')).toBeInTheDocument();
    expect(screen.getByText(/Suggestions will appear/)).toBeInTheDocument();
  });

  it('refreshes manually and applies enable or disable only after the server responds', async () => {
    const mutation = deferred<UserMemoryView>();
    vi.mocked(updateUserMemory).mockReturnValueOnce(mutation.promise);
    render(panel());
    const toggle = await screen.findByRole('switch');
    fireEvent.click(toggle);
    expect(updateUserMemory).toHaveBeenCalledWith(false, expect.any(AbortSignal));
    expect(toggle).toBeChecked();
    expect(toggle).toBeDisabled();
    await act(async () => mutation.resolve({ ...populated, enabled: false }));
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(/personalization are paused/)).toBeInTheDocument();

    vi.mocked(updateUserMemory).mockResolvedValueOnce(populated);
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toBeChecked());
    expect(updateUserMemory).toHaveBeenLastCalledWith(true, expect.any(AbortSignal));

    vi.mocked(getUserMemory).mockResolvedValueOnce(empty);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('No saved memory yet.');
    expect(getUserMemory).toHaveBeenCalledTimes(2);
  });

  it('requires explicit clear confirmation, supports cancellation, and displays the returned empty memory after clearing', async () => {
    render(panel());
    await screen.findByRole('switch');
    fireEvent.click(screen.getByRole('button', { name: 'Clear memory' }));
    expect(clearUserMemory).not.toHaveBeenCalled();
    expect(screen.getByText(/Old requests won’t be used again/)).toBeInTheDocument();
    expect(screen.getByText(/Project memory is kept/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confirm clear memory' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Clear memory' })).toHaveFocus();
    expect(screen.queryByRole('button', { name: 'Confirm clear memory' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear memory' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear memory' }));
    await screen.findByText('No saved memory yet.');
    expect(clearUserMemory).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(screen.queryByText(populated.memories[0].content)).not.toBeInTheDocument();
    expect(screen.queryByText(populated.suggestions[0].title)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm clear memory' })).not.toBeInTheDocument();
    expect(screen.queryByText(populated.portrait.summary)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Clear memory' })).toHaveFocus();
  });

  it('shows load errors with a retry and preserves saved settings when a change fails', async () => {
    vi.mocked(getUserMemory).mockRejectedValueOnce(new Error('Offline'));
    render(panel());
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load or update your memory',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    const toggle = await screen.findByRole('switch');
    vi.mocked(updateUserMemory).mockRejectedValueOnce(new Error('Unavailable'));
    fireEvent.click(toggle);
    await screen.findByRole('alert');
    expect(toggle).toBeChecked();
    expect(toggle).toBeEnabled();
    expect(screen.getByText(populated.memories[0].content)).toBeInTheDocument();
  });

  it('discards an old account response and aborts its fetch on an account switch', async () => {
    const oldRead = deferred<UserMemoryView>();
    const newRead = deferred<UserMemoryView>();
    vi.mocked(getUserMemory)
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(newRead.promise);
    const rendered = render(panel());
    const oldSignal = vi.mocked(getUserMemory).mock.calls[0][0]!;
    auth.userId = 'bob';
    rendered.rerender(panel());
    expect(oldSignal.aborted).toBe(true);
    await act(async () => oldRead.resolve(populated));
    expect(screen.queryByText(populated.portrait.summary)).not.toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    await act(async () => newRead.resolve(empty));
    expect(screen.getByText('No saved memory yet.')).toBeInTheDocument();
  });

  it('removes already displayed data immediately and ignores old mutations when accounts change', async () => {
    const oldClear = deferred<UserMemoryView>();
    const newRead = deferred<UserMemoryView>();
    vi.mocked(clearUserMemory).mockReturnValueOnce(oldClear.promise);
    const rendered = render(panel());
    await screen.findByText(populated.portrait.summary);
    fireEvent.click(screen.getByRole('button', { name: 'Clear memory' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear memory' }));
    const oldSignal = vi.mocked(clearUserMemory).mock.calls[0][0]!;
    vi.mocked(getUserMemory).mockReturnValueOnce(newRead.promise);
    auth.userId = 'bob';
    rendered.rerender(panel());
    expect(screen.queryByText(populated.portrait.summary)).not.toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(oldSignal.aborted).toBe(true);
    const otherAccount = { ...empty, enabled: false, avatar: illustratedAvatar('#8eaa68') };
    await act(async () => newRead.resolve(otherAccount));
    await act(async () => oldClear.resolve(populated));
    expect(screen.getByRole('switch')).not.toBeChecked();
    expect(screen.getByText('No saved memory yet.')).toBeInTheDocument();
    expect(screen.queryByText(populated.memories[0].content)).not.toBeInTheDocument();
  });

  it('aborts on unmount and does not fetch while signed out', async () => {
    const read = deferred<UserMemoryView>();
    vi.mocked(getUserMemory).mockReturnValueOnce(read.promise);
    const rendered = render(panel());
    const signal = vi.mocked(getUserMemory).mock.calls[0][0]!;
    rendered.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => read.resolve(populated));
    auth.userId = undefined;
    render(panel());
    expect(screen.getByText('Sign in to see your memory.')).toBeInTheDocument();
    expect(getUserMemory).toHaveBeenCalledTimes(1);
    expect(getAgentConfig).toHaveBeenCalledTimes(1);
  });

  it('translates memory controls and clear confirmation into Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(panel());
    await screen.findByRole('switch', { name: '自动建立记忆' });
    expect(screen.getByRole('combobox', { name: '整合模型' })).toHaveValue('model-a');
    expect(screen.getByRole('heading', { name: '用户记忆' })).toBeInTheDocument();
    expect(screen.getByText(/用户偏好适用于所有项目/)).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: '自动建立记忆' })).toBeChecked();
    expect(screen.getByRole('heading', { name: '未来任务建议' })).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '清除记忆' }));
    expect(screen.getByRole('button', { name: '确认清除记忆' })).toBeInTheDocument();
    expect(screen.getByText(/清除记忆会重置头像的工作主题，保留基础角色/)).toBeInTheDocument();
  });

  it('shows each memory’s source, project, creation date, and edited state', async () => {
    vi.mocked(getUserMemory).mockResolvedValue({
      ...populated,
      memories: [{ ...populated.memories[0], user_edited: true }, populated.memories[1]],
    });
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
    expect(within(first).getByText('Edited')).toBeInTheDocument();
    expect(within(first).getByText('Project: alice/toolbox')).toBeInTheDocument();
    expect(first.querySelector('time')).toHaveAttribute(
      'datetime',
      populated.memories[0].created_at,
    );
    expect(within(first).getByRole('link', { name: 'View source task' })).toHaveAttribute(
      'href',
      '/job%2Fone',
    );
    const second = screen.getByRole('listitem', { name: 'Saved memory 2' });
    expect(within(second).getByText('Project: No project')).toBeInTheDocument();
    expect(within(second).getByText('Date unavailable')).toBeInTheDocument();
  });

  it('edits only content, validates its length, restores focus on cancel, and saves while automatic updates are paused', async () => {
    const paused = { ...populated, enabled: false };
    vi.mocked(getUserMemory).mockResolvedValue(paused);
    const mutation = deferred<UserMemoryView>();
    vi.mocked(editUserMemoryEntry).mockReturnValueOnce(mutation.promise);
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
    fireEvent.click(within(first).getByRole('button', { name: 'Edit memory' }));
    let editor = within(first).getByRole('textbox', { name: 'Memory content' });
    expect(editor).toHaveFocus();
    expect(editor).toHaveValue(populated.memories[0].content);
    expect(editor).toHaveAttribute('maxlength', '240');
    fireEvent.change(editor, { target: { value: '   ' } });
    expect(within(first).getByRole('button', { name: 'Save memory' })).toBeDisabled();
    fireEvent.change(editor, { target: { value: 'x'.repeat(241) } });
    expect(within(first).getByRole('button', { name: 'Save memory' })).toBeDisabled();
    fireEvent.click(within(first).getByRole('button', { name: 'Cancel' }));
    expect(within(first).getByRole('button', { name: 'Edit memory' })).toHaveFocus();
    expect(editUserMemoryEntry).not.toHaveBeenCalled();

    fireEvent.click(within(first).getByRole('button', { name: 'Edit memory' }));
    editor = within(first).getByRole('textbox', { name: 'Memory content' });
    expect(editor).toHaveValue(populated.memories[0].content);
    fireEvent.change(editor, { target: { value: 'Prefer focused changes with tests.' } });
    fireEvent.click(within(first).getByRole('button', { name: 'Save memory' }));
    expect(editUserMemoryEntry).toHaveBeenCalledWith(
      'preference-1',
      'Prefer focused changes with tests.',
      populated.revision,
      expect.any(AbortSignal),
    );
    expect(editor).toBeDisabled();
    expect(within(first).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(
      within(screen.getByRole('listitem', { name: 'Saved memory 2' })).getByRole('button', {
        name: 'Remove memory',
      }),
    ).toBeDisabled();
    const saved = {
      ...paused,
      revision: 2,
      memories: [
        { ...paused.memories[0], content: 'Prefer focused changes with tests.', user_edited: true },
        paused.memories[1],
      ],
    };
    await act(async () => mutation.resolve(saved));
    expect(within(first).queryByRole('textbox')).not.toBeInTheDocument();
    expect(within(first).getByText('Edited')).toBeInTheDocument();
    expect(within(first).getByText('Project: alice/toolbox')).toBeInTheDocument();
    expect(within(first).getByText('Preference')).toBeInTheDocument();
    expect(within(first).getByRole('button', { name: 'Edit memory' })).toHaveFocus();
    expect(screen.getByRole('switch')).not.toBeChecked();
    vi.mocked(getUserMemory).mockResolvedValueOnce(saved);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(within(first).getByText('Prefer focused changes with tests.')).toBeInTheDocument();
  });

  it('preserves an edit draft on conflict, reviews refreshed saved text, and retries only after an explicit Save', async () => {
    vi.mocked(editUserMemoryEntry).mockRejectedValueOnce(
      new APIError('memory_conflict', 'server detail', 409),
    );
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
    fireEvent.click(within(first).getByRole('button', { name: 'Edit memory' }));
    fireEvent.change(within(first).getByRole('textbox', { name: 'Memory content' }), {
      target: { value: 'My unsaved preference.' },
    });
    fireEvent.click(within(first).getByRole('button', { name: 'Save memory' }));
    expect(await within(first).findByRole('alert')).toHaveTextContent(
      'Refresh to review the latest saved text, then save your draft again.',
    );
    expect(within(first).getByRole('textbox')).toHaveValue('My unsaved preference.');
    const latest = {
      ...populated,
      revision: 7,
      memories: [
        {
          ...populated.memories[0],
          content: 'Preference from another session.',
          user_edited: true,
        },
        populated.memories[1],
      ],
    };
    vi.mocked(getUserMemory).mockResolvedValueOnce(latest);
    fireEvent.click(within(first).getByRole('button', { name: 'Refresh memory' }));
    await within(first).findByText('Preference from another session.');
    expect(within(first).getByRole('textbox')).toHaveValue('My unsaved preference.');
    expect(within(first).getByRole('textbox')).toHaveFocus();
    expect(editUserMemoryEntry).toHaveBeenCalledTimes(1);
    vi.mocked(editUserMemoryEntry).mockResolvedValueOnce({
      ...latest,
      revision: 8,
      memories: [{ ...latest.memories[0], content: 'My unsaved preference.' }, latest.memories[1]],
    });
    fireEvent.click(within(first).getByRole('button', { name: 'Save memory' }));
    await waitFor(() => expect(within(first).queryByRole('textbox')).not.toBeInTheDocument());
    expect(editUserMemoryEntry).toHaveBeenLastCalledWith(
      'preference-1',
      'My unsaved preference.',
      7,
      expect.any(AbortSignal),
    );
  });

  it.each([422, 503])(
    'keeps drafts on %s errors and never reflects server validation or credential details',
    async (status) => {
      vi.mocked(editUserMemoryEntry).mockRejectedValueOnce(
        new APIError('validation_error', 'credential-secret-do-not-render', status),
      );
      render(panel());
      const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
      fireEvent.click(within(first).getByRole('button', { name: 'Edit memory' }));
      fireEvent.change(within(first).getByRole('textbox'), {
        target: { value: 'My draft remains.' },
      });
      fireEvent.click(within(first).getByRole('button', { name: 'Save memory' }));
      const alert = await within(first).findByRole('alert');
      expect(alert).toHaveTextContent(
        status === 422 ? 'leave out credentials' : 'Your draft is kept',
      );
      expect(screen.queryByText(/credential-secret-do-not-render/)).not.toBeInTheDocument();
      expect(within(first).getByRole('textbox')).toHaveValue('My draft remains.');
      expect(within(first).getByRole('button', { name: 'Save memory' })).toBeEnabled();
    },
  );

  it('requires entry removal confirmation, preserves the entry on failure, and focuses Saved memory after removal', async () => {
    vi.mocked(deleteUserMemoryEntry)
      .mockRejectedValueOnce(new APIError('unavailable', 'server detail', 503))
      .mockResolvedValueOnce({ ...populated, revision: 2, memories: [populated.memories[1]] });
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
    fireEvent.click(within(first).getByRole('button', { name: 'Remove memory' }));
    expect(deleteUserMemoryEntry).not.toHaveBeenCalled();
    expect(within(first).getByRole('button', { name: 'Confirm remove memory' })).toHaveFocus();
    fireEvent.click(within(first).getByRole('button', { name: 'Cancel' }));
    expect(within(first).getByRole('button', { name: 'Remove memory' })).toHaveFocus();
    fireEvent.click(within(first).getByRole('button', { name: 'Remove memory' }));
    fireEvent.click(within(first).getByRole('button', { name: 'Confirm remove memory' }));
    await within(first).findByRole('alert');
    expect(within(first).getByText(populated.memories[0].content)).toBeInTheDocument();
    fireEvent.click(within(first).getByRole('button', { name: 'Confirm remove memory' }));
    await waitFor(() =>
      expect(screen.queryByText(populated.memories[0].content)).not.toBeInTheDocument(),
    );
    expect(deleteUserMemoryEntry).toHaveBeenLastCalledWith(
      'preference-1',
      populated.revision,
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('heading', { name: 'Saved user memory' })).toHaveFocus();
    expect(screen.getByText(populated.memories[1].content)).toBeInTheDocument();
  });

  it('reviews changed memory after a removal conflict and confirms again with the refreshed revision', async () => {
    vi.mocked(deleteUserMemoryEntry).mockRejectedValueOnce(
      new APIError('memory_conflict', 'server detail', 409),
    );
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
    fireEvent.click(within(first).getByRole('button', { name: 'Remove memory' }));
    fireEvent.click(within(first).getByRole('button', { name: 'Confirm remove memory' }));
    expect(await within(first).findByRole('alert')).toHaveTextContent('then confirm removal again');
    const latest = {
      ...populated,
      revision: 5,
      memories: [
        { ...populated.memories[0], content: 'Updated preference from another session.' },
        populated.memories[1],
      ],
    };
    vi.mocked(getUserMemory).mockResolvedValueOnce(latest);
    fireEvent.click(within(first).getByRole('button', { name: 'Refresh memory' }));
    await within(first).findByText('Updated preference from another session.');
    expect(deleteUserMemoryEntry).toHaveBeenCalledTimes(1);
    expect(within(first).getByRole('button', { name: 'Confirm remove memory' })).toHaveFocus();
    vi.mocked(deleteUserMemoryEntry).mockResolvedValueOnce({
      ...latest,
      revision: 6,
      memories: [latest.memories[1]],
    });
    fireEvent.click(within(first).getByRole('button', { name: 'Confirm remove memory' }));
    await waitFor(() => expect(first).not.toBeInTheDocument());
    expect(deleteUserMemoryEntry).toHaveBeenLastCalledWith(
      'preference-1',
      5,
      expect.any(AbortSignal),
    );
  });

  it.each([404, 409])(
    'retains an unavailable draft for copying after a %s error and refresh, then closes it only on Cancel',
    async (status) => {
      vi.mocked(editUserMemoryEntry).mockRejectedValueOnce(
        new APIError('memory_error', 'private source detail', status),
      );
      render(panel());
      const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
      fireEvent.click(within(first).getByRole('button', { name: 'Edit memory' }));
      fireEvent.change(within(first).getByRole('textbox'), {
        target: { value: 'Still my draft.' },
      });
      fireEvent.click(within(first).getByRole('button', { name: 'Save memory' }));
      expect(await within(first).findByRole('alert')).toHaveTextContent(
        status === 404 ? 'This memory is no longer available' : 'Saved memory changed',
      );
      expect(within(first).getByRole('textbox')).toHaveValue('Still my draft.');
      expect(screen.queryByText('private source detail')).not.toBeInTheDocument();
      vi.mocked(getUserMemory).mockResolvedValueOnce({
        ...populated,
        revision: 3,
        memories: [populated.memories[1]],
      });
      fireEvent.click(within(first).getByRole('button', { name: 'Refresh memory' }));
      expect(await within(first).findByRole('alert')).toHaveTextContent(
        'Your draft is kept for copying',
      );
      expect(first).toBeInTheDocument();
      const editor = within(first).getByRole('textbox', {
        name: 'Memory content',
      }) as HTMLTextAreaElement;
      expect(editor).toHaveValue('Still my draft.');
      expect(editor).toBeEnabled();
      expect(editor).toHaveAttribute('readonly');
      editor.select();
      expect(editor.selectionStart).toBe(0);
      expect(editor.selectionEnd).toBe('Still my draft.'.length);
      expect(within(first).getByRole('button', { name: 'Save memory' })).toBeDisabled();
      expect(within(first).getByText('Project: alice/toolbox')).toBeInTheDocument();
      expect(within(first).getByText('Last saved text')).toBeInTheDocument();
      expect(editUserMemoryEntry).toHaveBeenCalledTimes(1);
      fireEvent.click(within(first).getByRole('button', { name: 'Cancel' }));
      expect(first).not.toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'Saved user memory' })).toHaveFocus();
    },
  );

  it('retains drafts when global Refresh clears every saved entry, and resets the retained draft and metadata on account switch', async () => {
    const rendered = render(panel());
    const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
    fireEvent.click(within(first).getByRole('button', { name: 'Edit memory' }));
    fireEvent.change(within(first).getByRole('textbox'), {
      target: { value: 'Unsaved private draft' },
    });
    vi.mocked(getUserMemory).mockResolvedValue(empty);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await within(first).findByRole('alert')).toHaveTextContent(
      'Your draft is kept for copying',
    );
    expect(within(first).getByRole('textbox')).toHaveValue('Unsaved private draft');
    expect(within(first).getByRole('textbox')).toBeEnabled();
    expect(within(first).getByRole('button', { name: 'Save memory' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(within(first).getByRole('textbox')).toHaveValue('Unsaved private draft');
    expect(editUserMemoryEntry).not.toHaveBeenCalled();

    auth.userId = 'bob';
    rendered.rerender(panel());
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    await screen.findByText('No saved memory yet.');
    expect(screen.queryByText('Project: alice/toolbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'View source task' })).not.toBeInTheDocument();
  });

  it('discards retained unavailable drafts only after intentional clear-all confirmation', async () => {
    render(panel());
    const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
    fireEvent.click(within(first).getByRole('button', { name: 'Edit memory' }));
    fireEvent.change(within(first).getByRole('textbox'), {
      target: { value: 'Draft to clear intentionally' },
    });
    vi.mocked(getUserMemory).mockResolvedValueOnce(empty);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await within(first).findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Clear memory' }));
    expect(within(first).getByRole('textbox')).toHaveValue('Draft to clear intentionally');
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear memory' }));
    await screen.findByText('No saved memory yet.');
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Clear memory' })).toHaveFocus();
  });

  it('explains the retained unavailable draft in Chinese and keeps it selectable with Save disabled', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(panel());
    const first = await screen.findByRole('listitem', { name: '已保存的记忆 1' });
    fireEvent.click(within(first).getByRole('button', { name: '编辑记忆' }));
    fireEvent.change(within(first).getByRole('textbox'), { target: { value: '尚未保存的草稿' } });
    vi.mocked(getUserMemory).mockResolvedValueOnce(empty);
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));
    expect(await within(first).findByRole('alert')).toHaveTextContent('草稿已保留以便复制');
    expect(within(first).getByRole('textbox', { name: '记忆内容' })).toHaveValue('尚未保存的草稿');
    expect(within(first).getByRole('textbox')).toBeEnabled();
    expect(within(first).getByRole('button', { name: '保存记忆' })).toBeDisabled();
    fireEvent.click(within(first).getByRole('button', { name: '取消' }));
    expect(screen.getByRole('heading', { name: '已保存的用户记忆' })).toHaveFocus();
  });

  it('aborts an entry edit and removes its draft immediately when the account changes', async () => {
    const edit = deferred<UserMemoryView>();
    const otherRead = deferred<UserMemoryView>();
    vi.mocked(editUserMemoryEntry).mockReturnValueOnce(edit.promise);
    const rendered = render(panel());
    const first = await screen.findByRole('listitem', { name: 'Saved memory 1' });
    fireEvent.click(within(first).getByRole('button', { name: 'Edit memory' }));
    fireEvent.change(within(first).getByRole('textbox'), {
      target: { value: 'Private account draft' },
    });
    fireEvent.click(within(first).getByRole('button', { name: 'Save memory' }));
    const signal = vi.mocked(editUserMemoryEntry).mock.calls[0][3]!;
    vi.mocked(getUserMemory).mockReturnValueOnce(otherRead.promise);
    auth.userId = 'bob';
    rendered.rerender(panel());
    expect(signal.aborted).toBe(true);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByText('Private account draft')).not.toBeInTheDocument();
    await act(async () => otherRead.resolve(empty));
    await act(async () =>
      edit.resolve({
        ...populated,
        memories: [
          { ...populated.memories[0], content: 'Private account draft', user_edited: true },
        ],
      }),
    );
    expect(screen.getByText('No saved memory yet.')).toBeInTheDocument();
    expect(screen.queryByText('Private account draft')).not.toBeInTheDocument();
  });

  it('translates editing, scope metadata, validation, and removal confirmation into Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(panel());
    const first = await screen.findByRole('listitem', { name: '已保存的记忆 1' });
    expect(within(first).getByText('项目：alice/toolbox')).toBeInTheDocument();
    fireEvent.click(within(first).getByRole('button', { name: '编辑记忆' }));
    expect(within(first).getByRole('textbox', { name: '记忆内容' })).toHaveFocus();
    expect(within(first).getByRole('button', { name: '保存记忆' })).toBeInTheDocument();
    fireEvent.click(within(first).getByRole('button', { name: '取消' }));
    fireEvent.click(within(first).getByRole('button', { name: '移除记忆' }));
    expect(within(first).getByRole('button', { name: '确认移除记忆' })).toHaveFocus();
    expect(within(first).getByText(/刷新记忆后，此条记忆也不会恢复/)).toBeInTheDocument();
  });
});

describe('bounded scoped memory', () => {
  it('submits the chosen model and keeps the choice when memory is refreshed', async () => {
    vi.mocked(consolidateUserMemory).mockResolvedValue(populated);
    render(panel());
    const selector = await screen.findByRole('combobox', { name: 'Consolidation model' });
    expect(selector).toHaveValue('model-a');
    expect(
      within(selector)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['model-a', 'model-b']);
    fireEvent.change(selector, { target: { value: 'model-b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Consolidate' }));
    await waitFor(() =>
      expect(consolidateUserMemory).toHaveBeenCalledWith(1, 'model-b', expect.any(AbortSignal)),
    );
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(screen.getByText(populated.memories[0].content)).toBeInTheDocument();
    expect(screen.getByText(populated.memories[1].content)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(selector).toHaveValue('model-b');
  });

  it('uses the preferred available model when the memory arrives after the catalog', async () => {
    const read = deferred<UserMemoryView>();
    vi.mocked(getUserMemory).mockReturnValueOnce(read.promise);
    vi.mocked(getAgentConfig).mockResolvedValue({
      ...agentConfig,
      models: ['model-b', 'model-a'],
    });
    render(panel());
    await act(async () => read.resolve(populated));
    expect(screen.getByRole('combobox', { name: 'Consolidation model' })).toHaveValue('model-a');
  });

  it('defaults to the first available model when the preferred model is unavailable', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue({
      ...agentConfig,
      models: ['model-b', 'model-c'],
    });
    render(panel());
    expect(await screen.findByRole('combobox', { name: 'Consolidation model' })).toHaveValue(
      'model-b',
    );
  });

  it('keeps other memory controls usable while models are loading and retries an empty catalog', async () => {
    const catalog = deferred<AgentConfigApi>();
    vi.mocked(getAgentConfig).mockReturnValueOnce(catalog.promise);
    render(panel());
    const selector = await screen.findByRole('combobox', { name: 'Consolidation model' });
    expect(selector).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Consolidate' })).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Loading available models');
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    expect(screen.getByRole('switch')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Clear memory' })).toBeEnabled();
    await act(async () => catalog.resolve({ ...agentConfig, models: [] }));
    expect(screen.getByRole('status')).toHaveTextContent('No models available for consolidation');
    expect(selector).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Consolidate' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    expect(consolidateUserMemory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry models' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Consolidate' })).toBeEnabled());
    expect(selector).toHaveValue('model-a');
    expect(selector).toBeEnabled();
    expect(getAgentConfig).toHaveBeenCalledTimes(2);
    expect(getUserMemory).toHaveBeenCalledTimes(1);
  });

  it('disables consolidation after a catalog error and can retry without reloading memory', async () => {
    vi.mocked(getAgentConfig).mockRejectedValueOnce(new Error('Offline'));
    render(panel());
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load models');
    expect(screen.getByRole('button', { name: 'Consolidate' })).toBeDisabled();
    expect(screen.getByRole('switch')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Clear memory' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry models' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Consolidate' })).toBeEnabled());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(getUserMemory).toHaveBeenCalledTimes(1);
    expect(getAgentConfig).toHaveBeenCalledTimes(2);
  });

  it('discards old catalog responses and resets model choices when the account changes', async () => {
    const oldCatalog = deferred<AgentConfigApi>();
    const newCatalog = deferred<AgentConfigApi>();
    vi.mocked(getAgentConfig)
      .mockReturnValueOnce(oldCatalog.promise)
      .mockReturnValueOnce(newCatalog.promise);
    const rendered = render(panel());
    await screen.findByRole('combobox', { name: 'Consolidation model' });
    auth.userId = 'bob';
    rendered.rerender(panel());
    await act(async () => newCatalog.resolve({ ...agentConfig, models: ['bob-model'] }));
    const selector = screen.getByRole('combobox', { name: 'Consolidation model' });
    expect(selector).toHaveValue('bob-model');
    await act(async () => oldCatalog.resolve({ ...agentConfig, models: ['alice-model'] }));
    expect(selector).toHaveValue('bob-model');
    expect(screen.queryByRole('option', { name: 'alice-model' })).not.toBeInTheDocument();
  });

  it('resets a selected model and aborts consolidation when the account changes', async () => {
    const consolidation = deferred<UserMemoryView>();
    vi.mocked(consolidateUserMemory).mockReturnValueOnce(consolidation.promise);
    const rendered = render(panel());
    const selector = await screen.findByRole('combobox', { name: 'Consolidation model' });
    fireEvent.change(selector, { target: { value: 'model-b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Consolidate' }));
    const oldSignal = vi.mocked(consolidateUserMemory).mock.calls[0][2]!;
    auth.userId = 'bob';
    vi.mocked(getUserMemory).mockResolvedValue({
      ...populated,
      portrait: { ...populated.portrait, preferred_model: 'bob-model' },
    });
    vi.mocked(getAgentConfig).mockResolvedValue({ ...agentConfig, models: ['bob-model'] });
    rendered.rerender(panel());
    expect(oldSignal.aborted).toBe(true);
    expect(await screen.findByRole('combobox', { name: 'Consolidation model' })).toHaveValue(
      'bob-model',
    );
    await act(async () => consolidation.resolve(empty));
    expect(screen.getByText(populated.memories[0].content)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Consolidation model' })).toHaveValue('bob-model');
  });

  it('consolidates the current revision and replaces entries with the returned scopes', async () => {
    vi.mocked(getUserMemory).mockResolvedValue(populated);
    vi.mocked(consolidateUserMemory).mockResolvedValue({
      ...populated,
      revision: 2,
      memories: [{ ...populated.memories[0], content: 'Keep reviews concise.', scope: 'user' }],
    });
    render(
      <LocaleProvider>
        <UserMemorySettings />
      </LocaleProvider>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Consolidate' }));
    await screen.findByText('Keep reviews concise.');
    expect(consolidateUserMemory).toHaveBeenCalledWith(1, 'model-a', expect.any(AbortSignal));
    expect(screen.queryByText('Fix the test runner.')).not.toBeInTheDocument();
    expect(screen.getByText(/1\/12 memories/)).toBeInTheDocument();
  });

  it('saves a user scope correction and disables consolidation while editing', async () => {
    vi.mocked(getUserMemory).mockResolvedValue(populated);
    vi.mocked(saveUserMemories).mockResolvedValue({
      ...populated,
      revision: 2,
      memories: [{ ...populated.memories[0], scope: 'user' }, populated.memories[1]],
    });
    render(
      <LocaleProvider>
        <UserMemorySettings />
      </LocaleProvider>,
    );
    const entry = await screen.findByRole('listitem', { name: 'Saved memory 1' });
    fireEvent.click(within(entry).getByRole('button', { name: 'Edit memory' }));
    expect(screen.getByRole('button', { name: 'Consolidate' })).toBeDisabled();
    fireEvent.change(within(entry).getByRole('combobox', { name: 'Applies to' }), {
      target: { value: 'user' },
    });
    expect(within(entry).getByRole('textbox')).toHaveAttribute('maxlength', '240');
    fireEvent.click(within(entry).getByRole('button', { name: 'Save memory' }));
    await waitFor(() =>
      expect(saveUserMemories).toHaveBeenCalledWith(
        1,
        [
          { id: 'preference-1', content: 'Use small pull requests.', scope: 'user' },
          { id: 'task-1', content: 'Fix the test runner.', scope: 'user' },
        ],
        expect.any(AbortSignal),
      ),
    );
  });
});
