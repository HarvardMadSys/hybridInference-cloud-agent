// The change stream client: frames become callbacks, a drop reconnects, and a
// deployment that pushes nothing ends the stream so the tab keeps its clocks.
import { afterEach, describe, expect, it, vi } from 'vitest';

import { streamAgentChanges, type StreamAgentChangesOptions } from './agentChanges';
import { fetchWithAuth } from './client';

vi.mock('./client', () => ({ fetchWithAuth: vi.fn() }));

function sse(...frames: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      // Split a frame across chunks: the reader must buffer to the boundary.
      const text = frames.join('');
      const middle = Math.floor(text.length / 2);
      controller.enqueue(encoder.encode(text.slice(0, middle)));
      controller.enqueue(encoder.encode(text.slice(middle)));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

function options(controller: AbortController): StreamAgentChangesOptions {
  return {
    signal: controller.signal,
    onSync: vi.fn(),
    onJob: vi.fn(),
    onThread: vi.fn(),
    onHint: vi.fn(),
    onDisconnect: vi.fn(() => controller.abort()),
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('streamAgentChanges', () => {
  it('turns frames into callbacks and reports the drop', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(
      sse(
        'event: hello\ndata: {"live":true}\n\n',
        ': keepalive\n\n',
        'event: thread\ndata: {"id":"athr_1","revision":2,"title":"Pushed"}\n\n',
        'event: job\ndata: {"id":"ajob_1","revision":3,"state":"succeeded"}\n\n',
        'event: usage\ndata: {"id":"ajob_1"}\n\n',
        'event: publication\ndata: {"id":"athr_1"}\n\n',
        'event: usage\ndata: {"id":"ajob_2","usage":{"ledger":{"status":"available"}}}\n\n',
        'event: resync\ndata: {"live":false}\n\n',
        'event: job\ndata: not json\n\n',
      ),
    );
    const controller = new AbortController();
    const opts = options(controller);

    await expect(streamAgentChanges(opts)).resolves.toBe('aborted');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/changes',
      expect.objectContaining({ signal: controller.signal }),
    );
    expect(opts.onSync).toHaveBeenNthCalledWith(1, true);
    expect(opts.onSync).toHaveBeenNthCalledWith(2, false);
    expect(opts.onThread).toHaveBeenCalledWith({ id: 'athr_1', revision: 2, title: 'Pushed' });
    expect(opts.onJob).toHaveBeenCalledTimes(1);
    expect(opts.onJob).toHaveBeenCalledWith({ id: 'ajob_1', revision: 3, state: 'succeeded' });
    expect(opts.onHint).toHaveBeenNthCalledWith(1, 'usage', 'ajob_1');
    expect(opts.onHint).toHaveBeenNthCalledWith(2, 'publication', 'athr_1');
    expect(opts.onHint).toHaveBeenNthCalledWith(3, 'usage', 'ajob_2', {
      ledger: { status: 'available' },
    });
    expect(opts.onDisconnect).toHaveBeenCalledTimes(1);
  });

  it.each([404, 401, 403])('stops when the deployment answers %i', async (status) => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(new Response('{}', { status }));
    const controller = new AbortController();
    const opts = options(controller);

    await expect(streamAgentChanges(opts)).resolves.toBe('unavailable');
    expect(opts.onSync).not.toHaveBeenCalled();
    expect(opts.onDisconnect).not.toHaveBeenCalled();
  });

  it('reconnects after a refused connection', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(fetchWithAuth)
        .mockRejectedValueOnce(new TypeError('network'))
        .mockResolvedValueOnce(sse('event: hello\ndata: {"live":true}\n\n'));
      const controller = new AbortController();
      const opts = options(controller);
      opts.onDisconnect = vi
        .fn()
        .mockImplementationOnce(() => undefined)
        .mockImplementationOnce(() => controller.abort());

      const done = streamAgentChanges(opts);
      await vi.runAllTimersAsync();

      await expect(done).resolves.toBe('aborted');
      expect(fetchWithAuth).toHaveBeenCalledTimes(2);
      expect(opts.onSync).toHaveBeenCalledWith(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('releases each reconnect abort listener on timeout and abort', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(fetchWithAuth).mockRejectedValue(new TypeError('network'));
      const controller = new AbortController();
      const add = vi.spyOn(controller.signal, 'addEventListener');
      const remove = vi.spyOn(controller.signal, 'removeEventListener');
      const opts = options(controller);
      opts.onDisconnect = vi.fn();
      const done = streamAgentChanges(opts);

      await vi.advanceTimersByTimeAsync(7_000);
      expect(fetchWithAuth).toHaveBeenCalledTimes(4);
      expect(add).toHaveBeenCalledTimes(4);
      expect(remove).toHaveBeenCalledTimes(3);
      controller.abort();
      await expect(done).resolves.toBe('aborted');
      expect(remove.mock.calls.map((call) => call[1])).toEqual(
        add.mock.calls.map((call) => call[1]),
      );
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
