// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  clearUserMemory,
  getUserMemory,
  updateUserMemory,
  type UserMemoryView,
} from '@/lib/api/userMemory';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { UserMemorySettings } from './UserMemorySettings';

const auth = vi.hoisted(() => ({ userId: 'alice' as string | undefined }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.userId ? { id: auth.userId } : null } }),
}));
vi.mock('@/lib/api/userMemory', () => ({
  getUserMemory: vi.fn(),
  updateUserMemory: vi.fn(),
  clearUserMemory: vi.fn(),
}));

const empty: UserMemoryView = {
  enabled: true,
  revision: 1,
  updated_at: null,
  avatar: { data_url: 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=', style: 'illustrated' },
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
      created_at: '2026-10-11T12:00:00Z',
    },
    {
      id: 'task-1',
      kind: 'task',
      content: 'Fix the test runner.',
      source_job_id: 'job_two',
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

  it('requires explicit clear confirmation, supports cancellation, and retains the avatar after clearing', async () => {
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
    expect(screen.getByRole('img')).toHaveAttribute('src', populated.avatar.data_url);
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
    await act(async () => newRead.resolve({ ...empty, enabled: false }));
    await act(async () => oldClear.resolve(populated));
    expect(screen.getByRole('switch')).not.toBeChecked();
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
    fireEvent.click(screen.getByRole('button', { name: '清除记忆' }));
    expect(screen.getByRole('button', { name: '确认清除记忆' })).toBeInTheDocument();
  });
});
