// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  clearUserMemory,
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
  getUserMemory: vi.fn(),
  updateUserMemory: vi.fn(),
  clearUserMemory: vi.fn(),
  editUserMemoryEntry: vi.fn(),
  deleteUserMemoryEntry: vi.fn(),
}));

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
  vi.mocked(getUserMemory).mockResolvedValue(populated);
  vi.mocked(updateUserMemory).mockResolvedValue({ ...populated, enabled: false });
  vi.mocked(clearUserMemory).mockResolvedValue(empty);
});

afterEach(cleanup);

describe('avatar and memory settings', () => {
  it('automatically loads an illustrated avatar, recent work, source-linked memory, and actionable suggestions', async () => {
    render(panel());
    expect(screen.getByRole('status')).toHaveTextContent('Loading your avatar and memory');
    const avatar = await screen.findByRole('img', {
      name: 'Your automatically generated illustrated avatar',
    });
    expect(avatar).toHaveAttribute('src', populated.avatar.data_url);
    expect(screen.getByRole('switch', { name: 'Automatically build memory' })).toBeChecked();
    expect(getUserMemory).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(screen.getByText(populated.portrait.summary)).toBeInTheDocument();
    expect(screen.getByText('alice/toolbox')).toBeInTheDocument();
    expect(screen.getByText('Testing')).toBeInTheDocument();
    expect(screen.getByText('codex')).toBeInTheDocument();
    expect(screen.getByText('model-a')).toBeInTheDocument();
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

  it('renders useful empty states while retaining the avatar', async () => {
    vi.mocked(getUserMemory).mockResolvedValue(empty);
    render(panel());
    await screen.findByRole('img');
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

  it('replaces the avatar with the refreshed work theme, preserves the paused response, and resets the theme after clearing', async () => {
    const refreshed: UserMemoryView = {
      ...populated,
      revision: 2,
      avatar: illustratedAvatar('#ac7db5'),
    };
    const refresh = deferred<UserMemoryView>();
    const paused = { ...refreshed, enabled: false };
    const cleared = { ...empty, enabled: false };
    vi.mocked(updateUserMemory).mockResolvedValueOnce(paused);
    vi.mocked(clearUserMemory).mockResolvedValueOnce(cleared);
    render(panel());
    const avatar = await screen.findByRole('img');
    expect(avatar).toHaveAttribute('src', populated.avatar.data_url);

    vi.mocked(getUserMemory).mockReturnValueOnce(refresh.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(avatar).toHaveAttribute('src', populated.avatar.data_url);
    await act(async () => refresh.resolve(refreshed));
    expect(avatar).toHaveAttribute('src', refreshed.avatar.data_url);

    fireEvent.click(screen.getByRole('switch', { name: 'Automatically build memory' }));
    await waitFor(() => expect(screen.getByRole('switch')).not.toBeChecked());
    expect(avatar).toHaveAttribute('src', paused.avatar.data_url);
    expect(screen.getByText(/Avatar theme updates, memory updates/)).toBeInTheDocument();
    vi.mocked(getUserMemory).mockResolvedValueOnce(paused);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(avatar).toHaveAttribute('src', paused.avatar.data_url);

    fireEvent.click(screen.getByRole('button', { name: 'Clear memory' }));
    expect(
      screen.getByText(/resets your avatar’s work theme while keeping its base character/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear memory' }));
    await screen.findByText('No saved memory yet.');
    expect(avatar).toHaveAttribute('src', cleared.avatar.data_url);
    expect(avatar).not.toHaveAttribute('src', refreshed.avatar.data_url);
    expect(screen.getByRole('switch')).not.toBeChecked();
  });

  it('requires explicit clear confirmation, supports cancellation, and displays the returned base avatar after clearing', async () => {
    render(panel());
    await screen.findByRole('img');
    fireEvent.click(screen.getByRole('button', { name: 'Clear memory' }));
    expect(clearUserMemory).not.toHaveBeenCalled();
    expect(screen.getByText(/Old requests won’t be used again/)).toBeInTheDocument();
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
    expect(screen.getByRole('img')).toHaveAttribute('src', empty.avatar.data_url);
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
    expect(screen.getByRole('img')).toHaveAttribute('src', otherAccount.avatar.data_url);
    expect(screen.getByRole('img')).not.toHaveAttribute('src', populated.avatar.data_url);
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
    expect(screen.getByText('Sign in to see your avatar and memory.')).toBeInTheDocument();
    expect(getUserMemory).toHaveBeenCalledTimes(1);
  });

  it('translates the avatar, memory controls, and clear confirmation into Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(panel());
    expect(await screen.findByRole('img', { name: '为你自动生成的插画头像' })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: '自动建立记忆' })).toBeChecked();
    expect(screen.getByRole('heading', { name: '未来任务建议' })).toBeInTheDocument();
    expect(
      screen.getByText('头像的基础角色保持不变，配色与细节会随近期工作变化。'),
    ).toBeInTheDocument();
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
    expect(editor).toHaveAttribute('maxlength', '500');
    fireEvent.change(editor, { target: { value: '   ' } });
    expect(within(first).getByRole('button', { name: 'Save memory' })).toBeDisabled();
    fireEvent.change(editor, { target: { value: 'x'.repeat(501) } });
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
    expect(screen.getByRole('heading', { name: 'Saved memory' })).toHaveFocus();
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
      expect(screen.getByRole('heading', { name: 'Saved memory' })).toHaveFocus();
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
    expect(screen.getByRole('heading', { name: '已保存的记忆' })).toHaveFocus();
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
