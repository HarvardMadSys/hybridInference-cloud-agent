// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  deletePendingMessage,
  editPendingMessage,
  getThreadQueue,
  resumeThreadQueue,
  sendPendingMessageNow,
  type PendingMessageApi,
  type ThreadQueueApi,
} from '@/lib/api/pendingMessages';
import { APIError } from '@/lib/utils/errors';

import {
  PUSH_BATCH_MS,
  PUSH_FALLBACK_POLL_MS,
  receiveHint,
  resetAgentPush,
  setAgentPushLive,
} from './agentPush';
import { PendingMessageQueue } from './PendingMessageQueue';
import { IDLE_QUEUE_POLL_MS, usePendingMessages } from './usePendingMessages';

vi.mock('@/lib/api/pendingMessages', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/pendingMessages')>()),
  getThreadQueue: vi.fn(),
  editPendingMessage: vi.fn(),
  deletePendingMessage: vi.fn(),
  resumeThreadQueue: vi.fn(),
  sendPendingMessageNow: vi.fn(),
}));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn() } }));

function message(overrides: Partial<PendingMessageApi> = {}): PendingMessageApi {
  return {
    id: 'apmsg_a',
    thread_id: 'athr_1',
    position: 1,
    state: 'queued',
    revision: 1,
    prompt: 'A: add a regression test',
    attachments: [],
    last_error: null,
    ...overrides,
  };
}

let server: ThreadQueueApi;

function queueWith(messages: PendingMessageApi[], extra: Partial<ThreadQueueApi> = {}) {
  server = {
    thread_id: 'athr_1',
    pending_messages: messages,
    queue_paused_reason: null,
    latest_job_id: 'ajob_1',
    latest_turn_no: 1,
    latest_job_state: 'running',
    ...extra,
  };
}

/** What the server holds after a write: the message as the write left it. */
function stored(row: PendingMessageApi): PendingMessageApi {
  server = {
    ...server,
    pending_messages: server.pending_messages.map((old) => (old.id === row.id ? row : old)),
  };
  return row;
}

/** The hook and the cards together, the way the composer uses them. */
function Harness({
  onSent,
  onQueue,
}: {
  onSent?: (jobId: string) => void;
  /** Hands the test the live queue, to read the server when it chooses. */
  onQueue?: (queue: ReturnType<typeof usePendingMessages>) => void;
}) {
  const queue = usePendingMessages({
    threadId: 'athr_1',
    active: true,
    onSent: onSent ? (jobId) => onSent(jobId) : undefined,
  });
  onQueue?.(queue);
  return <PendingMessageQueue queue={queue} />;
}

/** An edit the way the server takes it: at the row's revision, or refused. */
async function serverEdit(
  _threadId: string,
  messageId: string,
  revision: number,
  patch: { prompt?: string; state?: 'held' | 'queued' },
) {
  const row = server.pending_messages.find((candidate) => candidate.id === messageId)!;
  if (row.revision !== revision) {
    throw new APIError('revision_conflict', 'changed', 409, { current: structuredClone(row) });
  }
  return { pending: stored({ ...row, ...patch, revision: row.revision + 1 }), job: null };
}

beforeEach(() => {
  vi.resetAllMocks();
  queueWith([
    message(),
    message({ id: 'apmsg_b', position: 2, prompt: 'B: then update the changelog' }),
  ]);
  vi.mocked(getThreadQueue).mockImplementation(async () => structuredClone(server));
});
afterEach(() => {
  cleanup();
  resetAgentPush();
});

describe('PendingMessageQueue', () => {
  it('lists waiting messages in send order under one header, and says why one was refused', async () => {
    queueWith([
      message({ id: 'apmsg_b', position: 2, prompt: 'second' }),
      message({ id: 'apmsg_a', position: 1, prompt: 'first' }),
      message({
        id: 'apmsg_c',
        position: 3,
        prompt: 'refused',
        state: 'held',
        last_error: "Model 'gone' is not available to agent jobs on this deployment.",
      }),
    ]);
    render(<Harness />);

    const region = await screen.findByRole('region', { name: 'Queued messages' });
    const cards = within(region).getAllByRole('listitem');
    expect(cards.map((card) => card.getAttribute('data-message-id'))).toEqual([
      'apmsg_a',
      'apmsg_b',
      'apmsg_c',
    ]);
    // The count is said once, in the header, for all of them.
    expect(within(region).getByRole('button', { name: '3 pending' })).toBeInTheDocument();
    // A waiting message is just its text; only the refused one says more.
    expect(within(cards[0]).queryByText(/Not sent|Editing|Sending/)).toBeNull();
    expect(
      within(cards[2]).getByText(/^Not sent · Model 'gone' is not available/),
    ).toBeInTheDocument();
  });

  it('folds the list away and back under its header', async () => {
    render(<Harness />);
    const toggle = await screen.findByRole('button', { name: '2 pending' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryAllByRole('listitem')).toHaveLength(0);
    fireEvent.click(toggle);
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('holds a message while it is edited and saves it back into line', async () => {
    vi.mocked(editPendingMessage)
      .mockImplementationOnce(async () => ({
        pending: stored(message({ state: 'held', revision: 2 })),
        job: null,
      }))
      .mockImplementationOnce(async () => ({
        pending: stored(message({ revision: 3, prompt: 'A: add a regression test first' })),
        job: null,
      }));
    render(<Harness />);
    const card = (await screen.findAllByRole('listitem'))[0];

    fireEvent.click(within(card).getByRole('button', { name: 'Edit queued message' }));
    await waitFor(() =>
      expect(editPendingMessage).toHaveBeenCalledWith('athr_1', 'apmsg_a', 1, { state: 'held' }),
    );
    const editor = await within(card).findByRole('textbox', { name: 'Edit queued message' });
    fireEvent.change(editor, { target: { value: 'A: add a regression test first' } });
    // An IME's Enter commits the word, it does not save.
    fireEvent.keyDown(editor, { key: 'Enter', isComposing: true });
    expect(editPendingMessage).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(editor, { key: 'Enter' });

    await waitFor(() =>
      expect(editPendingMessage).toHaveBeenLastCalledWith('athr_1', 'apmsg_a', 2, {
        prompt: 'A: add a regression test first',
        state: 'queued',
      }),
    );
    await waitFor(() =>
      expect(within(card).queryByRole('textbox', { name: 'Edit queued message' })).toBeNull(),
    );
  });

  it('cancelling an edit lets the message go unchanged', async () => {
    vi.mocked(editPendingMessage)
      .mockImplementationOnce(async () => ({
        pending: stored(message({ state: 'held', revision: 2 })),
        job: null,
      }))
      .mockImplementationOnce(async () => ({
        pending: stored(message({ revision: 3 })),
        job: null,
      }));
    render(<Harness />);
    const card = (await screen.findAllByRole('listitem'))[0];
    fireEvent.click(within(card).getByRole('button', { name: 'Edit queued message' }));
    const editor = await within(card).findByRole('textbox', { name: 'Edit queued message' });
    fireEvent.change(editor, { target: { value: 'never saved' } });

    fireEvent.keyDown(editor, { key: 'Escape' });

    await waitFor(() =>
      expect(editPendingMessage).toHaveBeenLastCalledWith('athr_1', 'apmsg_a', 2, {
        state: 'queued',
      }),
    );
  });

  it('keeps the draft and shows what won when another tab changed the message first', async () => {
    const won = message({ revision: 3, prompt: 'from the other tab' });
    vi.mocked(editPendingMessage)
      .mockImplementationOnce(async () => ({
        pending: stored(message({ state: 'held', revision: 2 })),
        job: null,
      }))
      .mockImplementationOnce(async () => {
        // The other tab saved first; the server now holds its version.
        stored(won);
        throw new APIError('revision_conflict', 'changed', 409, { current: won });
      });
    render(<Harness />);
    const card = (await screen.findAllByRole('listitem'))[0];
    fireEvent.click(within(card).getByRole('button', { name: 'Edit queued message' }));
    const editor = await within(card).findByRole('textbox', { name: 'Edit queued message' });
    fireEvent.change(editor, { target: { value: 'my version' } });

    fireEvent.click(within(card).getByRole('button', { name: 'Save' }));

    expect(await within(card).findByRole('alert')).toHaveTextContent(/another tab/);
    expect(within(card).getByRole('textbox', { name: 'Edit queued message' })).toHaveValue(
      'my version',
    );
    expect(within(card).getByText('from the other tab')).toBeInTheDocument();
  });

  it('saves at the revision the draft was written against, not the one read since', async () => {
    vi.mocked(editPendingMessage).mockImplementation(serverEdit);
    let queue!: ReturnType<typeof usePendingMessages>;
    render(<Harness onQueue={(live) => (queue = live)} />);
    const card = (await screen.findAllByRole('listitem'))[0];
    fireEvent.click(within(card).getByRole('button', { name: 'Edit queued message' }));
    const editor = await within(card).findByRole('textbox', { name: 'Edit queued message' });
    fireEvent.change(editor, { target: { value: 'my version' } });

    // Another tab saves the same message, and this page reads it.
    const row = server.pending_messages[0];
    stored({ ...row, prompt: 'from the other tab', state: 'queued', revision: row.revision + 1 });
    await act(async () => {
      await queue.refresh();
    });
    expect(within(card).getByText('from the other tab')).toBeInTheDocument();
    fireEvent.click(within(card).getByRole('button', { name: 'Save' }));

    // Refused: the draft was written against the held revision, 2.
    expect(await within(card).findByRole('alert')).toHaveTextContent(/another tab/);
    expect(editPendingMessage).toHaveBeenLastCalledWith('athr_1', 'apmsg_a', 2, {
      prompt: 'my version',
      state: 'queued',
    });
    expect(server.pending_messages[0].prompt).toBe('from the other tab');
    expect(within(card).getByRole('textbox', { name: 'Edit queued message' })).toHaveValue(
      'my version',
    );

    // Having seen what won, saving again is a choice to replace it.
    fireEvent.click(within(card).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(within(card).queryByRole('textbox', { name: 'Edit queued message' })).toBeNull(),
    );
    expect(editPendingMessage).toHaveBeenLastCalledWith('athr_1', 'apmsg_a', 3, {
      prompt: 'my version',
      state: 'queued',
    });
    expect(server.pending_messages[0].prompt).toBe('my version');
  });

  it('deletes at the revision read, and the card goes', async () => {
    vi.mocked(deletePendingMessage).mockImplementation(async () => {
      server = { ...server, pending_messages: server.pending_messages.slice(1) };
      return { pending: null, job: null };
    });
    render(<Harness />);
    const card = (await screen.findAllByRole('listitem'))[0];

    fireEvent.click(within(card).getByRole('button', { name: 'Delete queued message' }));

    await waitFor(() => expect(deletePendingMessage).toHaveBeenCalledWith('athr_1', 'apmsg_a', 1));
    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(1));
    expect(screen.getByText('B: then update the changelog')).toBeInTheDocument();
  });

  it('says why a paused queue stopped and continues it on request', async () => {
    queueWith([message()], { queue_paused_reason: 'stopped', latest_job_state: 'cancelled' });
    vi.mocked(resumeThreadQueue).mockImplementation(async () => {
      queueWith([], { latest_job_id: 'ajob_2', latest_turn_no: 2 });
      return { pending: null, job: { id: 'ajob_2' } as never, queue_paused_reason: null };
    });
    const onSent = vi.fn();
    render(<Harness onSent={onSent} />);

    expect(await screen.findByText('Paused: stopped')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Continue sending queued messages' }));

    await waitFor(() => expect(resumeThreadQueue).toHaveBeenCalledWith('athr_1'));
    await waitFor(() => expect(onSent).toHaveBeenCalledWith('ajob_2'));
    await waitFor(() => expect(screen.queryByText('Paused: stopped')).toBeNull());
  });

  it('offers to add to the turn only while one that can take it is live', async () => {
    queueWith([message()], { mid_turn_input_supported: false });
    const { unmount } = render(<Harness />);
    await screen.findAllByRole('listitem');
    expect(screen.queryByRole('button', { name: 'Add to the turn in progress' })).toBeNull();
    unmount();

    // Still waiting for a host: it is given the message when it starts.
    queueWith([message()], { mid_turn_input_supported: true, latest_job_state: 'queued' });
    const second = render(<Harness />);
    expect(
      await screen.findByRole('button', { name: 'Add to the turn in progress' }),
    ).toBeEnabled();
    second.unmount();

    queueWith([message()], { mid_turn_input_supported: true, latest_job_state: 'running' });
    const third = render(<Harness />);
    expect(
      await screen.findByRole('button', { name: 'Add to the turn in progress' }),
    ).toBeEnabled();
    third.unmount();

    queueWith([message()], {
      mid_turn_input_supported: true,
      latest_job_state: 'cancelled',
      queue_paused_reason: 'stopped',
    });
    render(<Harness />);
    await screen.findAllByRole('listitem');
    expect(screen.queryByRole('button', { name: 'Add to the turn in progress' })).toBeNull();
  });

  it('adds a message to the live turn at the revision read, and it waits there until taken', async () => {
    queueWith([message(), message({ id: 'apmsg_b', position: 2, prompt: 'B' })], {
      mid_turn_input_supported: true,
    });
    vi.mocked(sendPendingMessageNow).mockImplementation(async () => ({
      pending: stored(message({ state: 'sending', revision: 2 })),
      job: null,
    }));
    const onSent = vi.fn();
    let queue: ReturnType<typeof usePendingMessages> | undefined;
    render(<Harness onSent={onSent} onQueue={(current) => (queue = current)} />);
    const card = (await screen.findAllByRole('listitem'))[0];

    fireEvent.click(within(card).getByRole('button', { name: 'Add to the turn in progress' }));

    await waitFor(() => expect(sendPendingMessageNow).toHaveBeenCalledWith('athr_1', 'apmsg_a', 1));
    // It goes in before the model's next tool call, and says so while it waits.
    const tag = await within(card).findByText('Adding');
    expect(tag).toHaveAttribute(
      'title',
      'Goes in before its next tool call; if this turn answers first, it is sent as the next turn.',
    );

    // The turn took it: the queue no longer has it.
    server = { ...server, pending_messages: server.pending_messages.slice(1) };
    await act(async () => {
      await queue!.refresh();
    });
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByText('B')).toBeInTheDocument();
    // It joined the turn: no new turn to follow.
    expect(onSent).not.toHaveBeenCalled();
  });

  it('a message the live turn cannot have stays in line and says why', async () => {
    queueWith([message()], { mid_turn_input_supported: true });
    vi.mocked(sendPendingMessageNow).mockImplementation(async () => ({
      pending: stored(message({ revision: 3 })),
      job: null,
      not_delivered_reason: 'unreachable',
    }));
    render(<Harness />);
    const card = (await screen.findAllByRole('listitem'))[0];

    fireEvent.click(within(card).getByRole('button', { name: 'Add to the turn in progress' }));

    expect(await within(card).findByRole('status')).toHaveTextContent(
      'Could not reach the host running this turn; it will be sent when the turn ends.',
    );
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
  });

  it('a message with files cannot join the running turn', async () => {
    queueWith(
      [
        message({
          attachments: [
            { id: 7, path: 'uploads/notes.txt', size_bytes: 5, content_type: 'text/plain' },
          ],
        }),
      ],
      { mid_turn_input_supported: true },
    );
    render(<Harness />);
    const card = (await screen.findAllByRole('listitem'))[0];

    const send = within(card).getByRole('button', { name: 'Add to the turn in progress' });

    expect(send).toBeDisabled();
    expect(send).toHaveAttribute(
      'title',
      'A message with files goes as a turn of its own; it cannot join the running one.',
    );
  });

  it("says in the owner's words why a message could not be offered to the turn", async () => {
    queueWith([message()], { mid_turn_input_supported: true });
    vi.mocked(sendPendingMessageNow).mockRejectedValue(
      new APIError('not_running', 'The turn is being stopped', 409),
    );
    render(<Harness />);
    const card = (await screen.findAllByRole('listitem'))[0];

    fireEvent.click(within(card).getByRole('button', { name: 'Add to the turn in progress' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This turn is stopping; the message stays in line.',
    );
  });
});

describe('usePendingMessages', () => {
  it('retains waiting messages after a failed read and clears the stale notice on retry', async () => {
    let queue!: ReturnType<typeof usePendingMessages>;
    render(
      <Harness
        onQueue={(value) => {
          queue = value;
        }}
      />,
    );
    await screen.findByText('A: add a regression test');
    vi.mocked(getThreadQueue).mockRejectedValueOnce(new Error('Offline'));
    await act(async () => queue.refresh());
    expect(screen.getByText('A: add a regression test')).toBeInTheDocument();
    expect(
      screen.getByText('Refresh failed — showing the last successful read.'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(queue.refreshError).toBeNull());
    expect(
      screen.queryByText('Refresh failed — showing the last successful read.'),
    ).not.toBeInTheDocument();
  });

  it('does not poll a hidden tab and reads immediately when visible again', async () => {
    vi.useFakeTimers();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    try {
      const { result } = renderHook(() => usePendingMessages({ threadId: 'athr_1', active: true }));
      await act(async () => {});
      const reads = vi.mocked(getThreadQueue).mock.calls.length;
      visibility.mockReturnValue('hidden');
      await act(async () => vi.advanceTimersByTimeAsync(10_000));
      expect(getThreadQueue).toHaveBeenCalledTimes(reads);
      expect(result.current.messages).toHaveLength(2);
      visibility.mockReturnValue('visible');
      await act(async () => document.dispatchEvent(new Event('visibilitychange')));
      expect(getThreadQueue).toHaveBeenCalledTimes(reads + 1);
    } finally {
      visibility.mockRestore();
      vi.useRealTimers();
    }
  });
  it.each([
    [false, IDLE_QUEUE_POLL_MS],
    [true, PUSH_FALLBACK_POLL_MS],
  ])(
    'retries an unread idle queue, then stops polling (push live: %s)',
    async (pushLive, interval) => {
      vi.useFakeTimers();
      try {
        queueWith([], { latest_job_state: 'succeeded' });
        vi.mocked(getThreadQueue).mockRejectedValue(new Error('Temporary read failure'));
        act(() => setAgentPushLive(pushLive));
        const { result } = renderHook(() =>
          usePendingMessages({ threadId: 'athr_1', active: false }),
        );
        await act(async () => {});
        expect(result.current.loaded).toBe(false);
        const reads = vi.mocked(getThreadQueue).mock.calls.length;

        vi.mocked(getThreadQueue).mockResolvedValue(structuredClone(server));
        await act(async () => vi.advanceTimersByTimeAsync(interval));
        expect(getThreadQueue).toHaveBeenCalledTimes(reads + 1);
        expect(result.current.loaded).toBe(true);
        expect(result.current.messages).toEqual([]);

        await act(async () => vi.advanceTimersByTimeAsync(interval * 2));
        expect(getThreadQueue).toHaveBeenCalledTimes(reads + 1);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it('reports a message the server sent, with the turn it follows', async () => {
    const onSent = vi.fn();
    const { result } = renderHook(() =>
      usePendingMessages({ threadId: 'athr_1', active: true, onSent }),
    );
    await waitFor(() => expect(result.current.messages).toHaveLength(2));

    // The running turn ends; the server sends A as the next turn.
    queueWith([message({ id: 'apmsg_b', position: 2, prompt: 'B' })], {
      latest_job_id: 'ajob_2',
      latest_turn_no: 2,
      latest_job_state: 'queued',
    });
    await act(async () => {
      await result.current.refresh();
    });

    expect(onSent).toHaveBeenCalledExactlyOnceWith('ajob_2', { requested: false, after: 'ajob_1' });
    expect(result.current.messages.map((row) => row.id)).toEqual(['apmsg_b']);
    // Reading the same state again reports nothing new.
    await act(async () => {
      await result.current.refresh();
    });
    expect(onSent).toHaveBeenCalledTimes(1);
  });

  it('reports a message this page sent even when no read saw it waiting', async () => {
    queueWith([]);
    const onSent = vi.fn();
    const { result } = renderHook(() =>
      usePendingMessages({ threadId: 'athr_1', active: true, onSent }),
    );
    await waitFor(() => expect(result.current.loaded).toBe(true));

    // Sent while ajob_1 runs; ajob_1 ends before the next read, and the server
    // sends the message as the next turn.
    act(() => {
      result.current.apply({ pending: message(), job: null });
    });
    expect(result.current.messages.map((row) => row.id)).toEqual(['apmsg_a']);
    queueWith([], { latest_job_id: 'ajob_2', latest_turn_no: 2, latest_job_state: 'queued' });
    await act(async () => {
      await result.current.refresh();
    });

    expect(onSent).toHaveBeenCalledExactlyOnceWith('ajob_2', { requested: false, after: 'ajob_1' });
    expect(result.current.messages).toEqual([]);
  });

  it('does not mistake a message this page deleted for one that was sent', async () => {
    const onSent = vi.fn();
    vi.mocked(deletePendingMessage).mockResolvedValue({ pending: null, job: null });
    const { result } = renderHook(() =>
      usePendingMessages({ threadId: 'athr_1', active: true, onSent }),
    );
    await waitFor(() => expect(result.current.messages).toHaveLength(2));

    await act(async () => {
      await result.current.remove(result.current.messages[0]);
    });
    // A goal round appeared meanwhile: a newer turn, but not this message.
    queueWith([message({ id: 'apmsg_b', position: 2 })], { latest_job_id: 'ajob_round' });
    await act(async () => {
      await result.current.refresh();
    });

    expect(onSent).not.toHaveBeenCalled();
  });

  it('never lets an older read put back a queue a newer answer replaced', async () => {
    let answer!: (queue: ThreadQueueApi) => void;
    const { result } = renderHook(() => usePendingMessages({ threadId: 'athr_1', active: false }));
    await waitFor(() => expect(result.current.loaded).toBe(true));
    vi.mocked(getThreadQueue).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    const slow = result.current.refresh();
    act(() => {
      result.current.apply({ pending: message({ id: 'apmsg_new', position: 9 }), job: null });
    });
    await act(async () => {
      answer({ ...server, pending_messages: [] });
      await slow;
    });

    expect(result.current.messages.map((row) => row.id)).toContain('apmsg_new');
  });
});

describe('the queue with the owner change stream', () => {
  it('reads a pushed change to its queue at once and drops the two-second clock', async () => {
    vi.useFakeTimers();
    try {
      act(() => setAgentPushLive(true));
      renderHook(() => usePendingMessages({ threadId: 'athr_1', active: true }));
      await act(async () => {});
      const reads = vi.mocked(getThreadQueue).mock.calls.length;

      await act(async () => vi.advanceTimersByTimeAsync(5_000));
      expect(getThreadQueue).toHaveBeenCalledTimes(reads);

      await act(async () => {
        receiveHint('queue', 'athr_1');
        await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
      });
      expect(getThreadQueue).toHaveBeenCalledTimes(reads + 1);

      await act(async () => {
        receiveHint('queue', 'athr_other');
        await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
      });
      expect(getThreadQueue).toHaveBeenCalledTimes(reads + 1);
    } finally {
      vi.useRealTimers();
    }
  });
});
