// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentConfig } from '@/lib/api/agents';
import { uploadAttachment } from '@/lib/api/attachments';
import {
  getThreadQueue,
  sendPendingMessageNow,
  submitThreadMessage,
  type PendingMessageApi,
  type ThreadMessageResultApi,
  type ThreadQueueApi,
} from '@/lib/api/pendingMessages';

import { JobDetail } from './JobDetail';
import type { AgentJob } from './types';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
  usePathname: () => '/job-first',
}));

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getAgentConfig: vi.fn(),
  getAgentJobUsage: vi.fn().mockResolvedValue(null),
  refreshAgentJobUsage: vi.fn().mockResolvedValue(null),
  getAgentThreadStats: vi.fn().mockResolvedValue(null),
  listAgentTerminals: vi.fn().mockResolvedValue([]),
  getAgentJobFiles: vi.fn().mockResolvedValue({ path: '', kind: 'directory', entries: [] }),
  getAgentJobGit: vi.fn().mockResolvedValue({ available: false }),
}));

vi.mock('@/lib/api/mcp', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/mcp')>()),
  listMcpInvocations: vi.fn().mockResolvedValue({ invocations: [] }),
  getMcpThreadBindings: vi.fn().mockResolvedValue({ bindings: [] }),
  listMcpConnections: vi.fn().mockResolvedValue({ connections: [] }),
}));

vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi.fn().mockResolvedValue({ questions: [], attempt_id: 1 }),
  answerThreadQuestion: vi.fn(),
}));

vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn().mockResolvedValue({ skills: [] }) }));

vi.mock('@/lib/api/attachments', () => ({
  uploadAttachment: vi.fn(),
  deleteAttachment: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('@/lib/api/pendingMessages', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/pendingMessages')>()),
  getThreadQueue: vi.fn(),
  submitThreadMessage: vi.fn(),
  sendPendingMessageNow: vi.fn(),
}));

function job(threadId = 'thread-first', overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: `job-${threadId}`,
    threadId,
    title: 'Current task',
    prompt: 'Fix the issue',
    state: 'running',
    repo: 'owner/repository',
    baseRef: 'dev',
    baseSha: 'abcdef1',
    branch: 'agent/task',
    runtime: 'claude-code',
    model: 'qwen-test',
    spentUsd: 0,
    hasLedger: false,
    timeoutLabel: '',
    sandbox: 'container',
    attempts: [{ no: 1, status: 'live' }],
    currentAttemptNo: 1,
    events: [],
    diffFiles: [],
    diffStat: { add: 0, del: 0 },
    gates: [],
    usage: { tokensIn: '0', tokensOut: '0', cachePct: 0, turns: 1 },
    ...overrides,
  };
}

function queue(threadId: string, overrides: Partial<ThreadQueueApi> = {}): ThreadQueueApi {
  return {
    thread_id: threadId,
    pending_messages: [],
    queue_paused_reason: null,
    latest_job_id: `job-${threadId}`,
    latest_turn_no: 1,
    latest_job_state: 'running',
    mid_turn_input_supported: true,
    ...overrides,
  };
}

function pending(prompt: string, overrides: Partial<PendingMessageApi> = {}): PendingMessageApi {
  return {
    id: 'pending-new',
    thread_id: 'thread-first',
    position: 1,
    state: 'queued',
    revision: 1,
    prompt,
    attachments: [],
    last_error: null,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.clearAllMocks();
  vi.mocked(getAgentConfig).mockResolvedValue({
    repos: ['owner/repository'],
    runtimes: ['claude-code'],
    models: ['qwen-test'],
    github_connected: true,
    github_install_url: null,
  });
  vi.mocked(getThreadQueue)
    .mockReset()
    .mockImplementation(async (threadId) => queue(threadId));
  vi.mocked(submitThreadMessage).mockReset();
  vi.mocked(sendPendingMessageNow).mockReset();
  vi.mocked(uploadAttachment).mockReset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('conversation composer continuity', () => {
  it('restores drafts after a page remount and keeps account/chat navigation isolated', async () => {
    const first = render(<JobDetail job={job()} userId="alice" />);
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: '  My unfinished task\nwith context  ' },
    });
    first.unmount();
    const next = render(<JobDetail job={job()} userId="alice" />);
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue(
      '  My unfinished task\nwith context  ',
    );
    next.rerender(<JobDetail job={job('thread-second')} userId="alice" />);
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('');
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'A different task' },
    });
    next.rerender(<JobDetail job={job()} userId="bob" />);
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('');
    next.rerender(<JobDetail job={job()} userId="alice" />);
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue(
      '  My unfinished task\nwith context  ',
    );
    next.rerender(<JobDetail job={job('thread-second')} userId="alice" />);
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('A different task');
    await waitFor(() => expect(getThreadQueue).toHaveBeenCalledWith('thread-second'));
  });

  it('keeps text typed during a submission and saves it for the next page visit', async () => {
    const request = deferred<ThreadMessageResultApi>();
    vi.mocked(submitThreadMessage).mockReturnValue(request.promise);
    const view = render(<JobDetail job={job()} userId="alice" />);
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'First message' } });
    fireEvent.click(screen.getByRole('button', { name: 'Queue' }));
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledOnce());
    expect(box).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Queue' })).toBeDisabled();
    fireEvent.change(box, { target: { value: 'Next message, typed while waiting' } });
    await act(async () => request.resolve({ pending: pending('First message'), job: null }));
    expect(box).toHaveValue('Next message, typed while waiting');
    expect(submitThreadMessage).toHaveBeenCalledWith('thread-first', { prompt: 'First message' });
    const waiting = screen.getByRole('region', { name: 'Queued messages' });
    expect(within(waiting).getByText('First message')).toBeInTheDocument();
    view.unmount();
    render(<JobDetail job={job()} userId="alice" />);
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue(
      'Next message, typed while waiting',
    );
  });

  it('retains a revised draft after a failed send and clears it only after retry succeeds', async () => {
    const request = deferred<ThreadMessageResultApi>();
    vi.mocked(submitThreadMessage)
      .mockReturnValueOnce(request.promise)
      .mockResolvedValue({ pending: pending('Revised message'), job: null });
    const view = render(<JobDetail job={job()} userId="alice" />);
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'First message' } });
    fireEvent.click(screen.getByRole('button', { name: 'Queue' }));
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledOnce());
    fireEvent.change(box, { target: { value: 'Revised message' } });
    await act(async () => request.reject(new Error('Connection interrupted')));
    expect(await screen.findByText('Connection interrupted')).toBeInTheDocument();
    expect(box).toHaveValue('Revised message');
    fireEvent.click(screen.getByRole('button', { name: 'Queue' }));
    await waitFor(() => expect(box).toHaveValue(''));
    expect(submitThreadMessage).toHaveBeenLastCalledWith('thread-first', {
      prompt: 'Revised message',
    });
    view.unmount();
    render(<JobDetail job={job()} userId="alice" />);
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('');
  });

  it('keeps a late submission from inserting a message into the chat opened during the request', async () => {
    const request = deferred<ThreadMessageResultApi>();
    vi.mocked(submitThreadMessage).mockReturnValue(request.promise);
    const view = render(<JobDetail job={job()} userId="alice" />);
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Sent from the first chat' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Queue' }));
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledOnce());
    view.rerender(<JobDetail job={job('thread-second')} userId="alice" />);
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'The second chat’s draft' },
    });
    await act(async () =>
      request.resolve({ pending: pending('Sent from the first chat'), job: null }),
    );
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('The second chat’s draft');
    expect(screen.queryByRole('region', { name: 'Queued messages' })).not.toBeInTheDocument();
    expect(navigation.push).not.toHaveBeenCalled();
    view.rerender(<JobDetail job={job()} userId="alice" />);
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('');
  });

  it('acknowledges a successful send after unmount without navigating away from the new chat', async () => {
    const request = deferred<ThreadMessageResultApi>();
    vi.mocked(submitThreadMessage).mockReturnValue(request.promise);
    const first = render(<JobDetail job={job('thread-first', { state: 'done' })} userId="alice" />);
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Start the next turn' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledOnce());
    first.unmount();
    window.history.pushState(null, '', '/job-thread-second');
    const push = vi.spyOn(window.history, 'pushState');
    const second = render(<JobDetail job={job('thread-second')} userId="alice" />);
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Stay in this chat' },
    });
    await act(async () =>
      request.resolve({
        pending: null,
        job: { id: 'job-created' } as NonNullable<ThreadMessageResultApi['job']>,
      }),
    );
    expect(push).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe('/job-thread-second');
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('Stay in this chat');
    expect(screen.queryByRole('region', { name: 'Queued messages' })).not.toBeInTheDocument();
    second.unmount();
    render(<JobDetail job={job('thread-first', { state: 'done' })} userId="alice" />);
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('');
  });

  it('does not continue steering a queued draft after the composer unmounts', async () => {
    const request = deferred<ThreadMessageResultApi>();
    vi.mocked(submitThreadMessage).mockReturnValue(request.promise);
    const first = render(<JobDetail job={job()} userId="alice" />);
    const steer = await screen.findByRole('button', { name: 'Steer now' });
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Change direction' },
    });
    fireEvent.click(steer);
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledOnce());
    first.unmount();
    render(<JobDetail job={job('thread-second')} userId="alice" />);
    await act(async () => request.resolve({ pending: pending('Change direction'), job: null }));
    expect(sendPendingMessageNow).not.toHaveBeenCalled();
    expect(screen.queryByRole('region', { name: 'Queued messages' })).not.toBeInTheDocument();
  });
});

describe('visible send modes', () => {
  it('steers only the new draft and leaves an older queued message waiting', async () => {
    const older = pending('Run this after the turn', { id: 'pending-old' });
    const drafted = pending('Change direction now', { position: 2 });
    vi.mocked(getThreadQueue).mockImplementation(async (threadId) =>
      queue(threadId, { pending_messages: [older] }),
    );
    vi.mocked(submitThreadMessage).mockResolvedValue({ pending: drafted, job: null });
    vi.mocked(sendPendingMessageNow).mockResolvedValue({
      pending: { ...drafted, state: 'sending', revision: 2 },
      job: null,
    });
    render(<JobDetail job={job()} userId="alice" />);
    const steer = await screen.findByRole('button', { name: 'Steer now' });
    expect(screen.getByRole('button', { name: 'Queue' })).toBeVisible();
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Change direction now' },
    });
    fireEvent.click(steer);
    await waitFor(() =>
      expect(sendPendingMessageNow).toHaveBeenCalledWith('thread-first', 'pending-new', 1),
    );
    expect(sendPendingMessageNow).toHaveBeenCalledOnce();
    expect(submitThreadMessage).toHaveBeenCalledWith('thread-first', {
      prompt: 'Change direction now',
    });
    const waiting = screen.getByRole('region', { name: 'Queued messages' });
    expect(within(waiting).getByText('Run this after the turn')).toBeInTheDocument();
  });

  it.each([
    {
      state: 'running' as const,
      supported: false,
      latestState: 'running' as const,
      label: 'Queue',
    },
    { state: 'done' as const, supported: true, latestState: 'succeeded' as const, label: 'Send' },
  ])('offers $label without steering for state=$state and support=$supported', async (mode) => {
    vi.mocked(getThreadQueue).mockImplementation(async (threadId) =>
      queue(threadId, {
        mid_turn_input_supported: mode.supported,
        latest_job_state: mode.latestState,
      }),
    );
    render(<JobDetail job={job('thread-first', { state: mode.state })} userId="alice" />);
    await waitFor(() => expect(getThreadQueue).toHaveBeenCalled());
    expect(screen.getByRole('button', { name: mode.label })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Steer now' })).not.toBeInTheDocument();
  });

  it('hides steering while a file is uploading or attached and queues the file on the shortcut', async () => {
    const upload = deferred<Awaited<ReturnType<typeof uploadAttachment>>>();
    vi.mocked(uploadAttachment).mockReturnValue(upload.promise);
    const attachment = { id: 42, path: 'spec.txt', size_bytes: 12, content_type: 'text/plain' };
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: pending('Use this spec', { attachments: [attachment] }),
      job: null,
    });
    const { container } = render(<JobDetail job={job()} userId="alice" />);
    await screen.findByRole('button', { name: 'Steer now' });
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'Use this spec' } });
    fireEvent.change(container.querySelector<HTMLInputElement>('input[type="file"]')!, {
      target: { files: [new File(['requirements'], 'spec.txt')] },
    });
    expect(screen.queryByRole('button', { name: 'Steer now' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Queue' })).toBeDisabled();
    await act(async () => upload.resolve(attachment));
    expect(screen.getByRole('button', { name: 'Queue' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Steer now' })).not.toBeInTheDocument();
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true });
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('thread-first', {
        prompt: 'Use this spec',
        attachment_ids: [42],
      }),
    );
    expect(sendPendingMessageNow).not.toHaveBeenCalled();
  });
});
