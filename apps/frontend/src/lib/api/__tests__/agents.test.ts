// Tests for the agent-jobs API client.
//
// The interesting behaviour is all in the stream reader: frame parsing across
// chunk boundaries, cursor tracking, and reconnect-without-gap. Those are the
// things that silently lose a user's event log if they are wrong.

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import {
  archiveAgentJob,
  clearAgentGoal,
  connectGitHub,
  connectGitLab,
  createAgentTerminal,
  deleteAgentTerminal,
  disconnectAgentIntegration,
  createAgentJob,
  forkAgentJob,
  getAgentGoal,
  getAgentIntegrations,
  getAgentJob,
  getAgentAttemptTelemetry,
  getAgentJobFiles,
  getAgentJobGit,
  getAgentJobUsage,
  getAgentJobThread,
  listAllAgentThreadEvents,
  listAgentConversations,
  listAgentTerminals,
  pauseAgentGoal,
  pinAgentJob,
  renameAgentJob,
  resizeAgentTerminal,
  resumeAgentGoal,
  refreshAgentJobUsage,
  restoreAgentJob,
  setAgentGoal,
  streamAgentTerminal,
  streamAgentJob,
  unpinAgentJob,
  writeAgentTerminalInput,
  writeAgentJobFile,
} from '../agents';
import * as client from '../client';

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[index++]));
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function frame(id: number, eventType: string, payload: Record<string, unknown>): string {
  const data = JSON.stringify({
    id,
    attempt_id: 1,
    seq: id,
    event_type: eventType,
    payload,
    created_at: null,
  });
  return `id: ${id}\nevent: ${eventType}\ndata: ${data}\n\n`;
}

const FINISHED = 'event: job_finished\ndata: {"state":"succeeded","published_pr_url":"u"}\n\n';

describe('agents api', () => {
  let fetchWithAuth: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchWithAuth = vi.spyOn(client, 'fetchWithAuth');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renames a conversation via its job without modifying the prompt', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ thread_id: 'thread-1', title: 'New title' }));
    await expect(renameAgentJob('job/1', 'New title')).resolves.toEqual({
      thread_id: 'thread-1',
      title: 'New title',
    });
    expect(fetchWithAuth).toHaveBeenCalledWith(expect.any(String), '/v1/agent/jobs/job%2F1/title', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'New title' }),
    });
  });

  it.each([true, false, undefined])(
    'sends the pin filter %s without conflating false with omitted',
    async (pinned) => {
      fetchWithAuth.mockResolvedValue(jsonResponse({ conversations: [], next_cursor: null }));
      await listAgentConversations({ pinned });
      expect(fetchWithAuth).toHaveBeenCalledWith(
        expect.any(String),
        `/v1/agent/conversations?limit=50${pinned === undefined ? '' : `&pinned=${pinned}`}`,
        { signal: undefined },
      );
    },
  );

  it('forwards cancellation for conversation reads without changing query filters', async () => {
    const controller = new AbortController();
    fetchWithAuth.mockImplementation(
      (_base: string, _path: string, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(controller.signal.reason), {
            once: true,
          });
        }),
    );
    const request = listAgentConversations(
      { q: 'fix bugs', repo: 'owner/repo', includeArchived: true, cursor: 'next', limit: 200 },
      controller.signal,
    );
    const cancellation = expect(request).rejects.toMatchObject({ name: 'AbortError' });
    controller.abort();
    await cancellation;
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/conversations?limit=200&q=fix+bugs&repo=owner%2Frepo&include_archived=true&cursor=next',
      { signal: controller.signal },
    );
  });

  it('pins legacy telemetry identity when fetching a snapshot', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ current_attempt_id: 10 }));
    expect((await getAgentJob('ajob_1')).telemetry_attempt_id).toBe(10);
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/ajob_1?include_resources=false',
    );
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ current_attempt_id: 11, telemetry_attempt_id: 10 }),
    );
    expect((await getAgentJob('ajob_1')).telemetry_attempt_id).toBe(10);
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ current_attempt_id: 11, telemetry_attempt_id: null }),
    );
    expect((await getAgentJob('ajob_1')).telemetry_attempt_id).toBeNull();
  });

  it('fetches one selected attempt with an abortable owner-scoped request', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ attempt_id: 10, resources: null }));
    const { signal } = new AbortController();
    expect((await getAgentAttemptTelemetry('job/one', 10, signal)).attempt_id).toBe(10);
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/attempts/10/telemetry',
      { signal },
    );
  });

  it('reads and refreshes owner-scoped usage without rounding decimal costs', async () => {
    const body = { ledger: { totals: { spent_usd: '0.12345678' } } };
    fetchWithAuth.mockResolvedValue(jsonResponse(body));
    const { signal } = new AbortController();
    expect((await getAgentJobUsage('job/one', signal))?.ledger.totals?.spent_usd).toBe(
      '0.12345678',
    );
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/usage',
      { signal },
    );
    fetchWithAuth.mockResolvedValue(jsonResponse(body));
    expect(await refreshAgentJobUsage('job/one', signal)).toEqual(body);
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/usage/refresh',
      { method: 'POST', signal },
    );
  });

  it('treats a pre-ledger 404 as unavailable while preserving real API failures', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ detail: 'Not Found' }, 404));
    expect(await getAgentJobUsage('ajob_1')).toBeNull();
    expect(await refreshAgentJobUsage('ajob_1')).toBeNull();
    fetchWithAuth.mockResolvedValue(jsonResponse({ detail: 'Unavailable' }, 503));
    await expect(getAgentJobUsage('ajob_1')).rejects.toMatchObject({ statusCode: 503 });
  });

  it('reads every page of a conversation event log', async () => {
    const first = Array.from({ length: 1000 }, (_, index) => ({
      id: index + 1,
      job_id: 'ajob_1',
      turn_no: 1,
      attempt_id: 1,
      seq: index + 1,
      event_type: 'message',
      payload: {},
      created_at: null,
    }));
    fetchWithAuth
      .mockResolvedValueOnce(jsonResponse({ events: first, next_cursor: 1000 }))
      .mockResolvedValueOnce(
        jsonResponse({ events: [{ ...first[0], id: 1001, turn_no: 2 }], next_cursor: 1001 }),
      );

    const events = await listAllAgentThreadEvents('job/one');

    expect(events).toHaveLength(1001);
    expect(events[1000].turn_no).toBe(2);
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/thread/events?after=1000&limit=1000',
    );
  });

  it('reads a conversation event log from a held position to its end', async () => {
    fetchWithAuth.mockResolvedValueOnce(jsonResponse({ events: [{ id: 43 }], next_cursor: 43 }));

    const events = await listAllAgentThreadEvents('job/one', { after: 42 });

    expect(events).toEqual([{ id: 43 }]);
    expect(fetchWithAuth).toHaveBeenCalledOnce();
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/thread/events?after=42&limit=1000',
    );
  });

  it('publishes immutable activity snapshots and forwards cancellation to every page', async () => {
    const first = Array.from({ length: 1000 }, (_, index) => ({ id: index + 1 }));
    const last = { id: 1001 };
    fetchWithAuth
      .mockResolvedValueOnce(jsonResponse({ events: first, next_cursor: 1000 }))
      .mockResolvedValueOnce(jsonResponse({ events: [last], next_cursor: 1001 }));
    const controller = new AbortController();
    const onPage = vi.fn();
    const result = await listAllAgentThreadEvents('job/one', { signal: controller.signal, onPage });
    expect(onPage).toHaveBeenCalledTimes(2);
    expect(onPage.mock.calls[0][0]).toEqual(first);
    expect(onPage.mock.calls[1][0]).toEqual([...first, last]);
    expect(result).toEqual([...first, last]);
    expect(onPage.mock.calls[0][0]).not.toBe(onPage.mock.calls[1][0]);
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      1,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/thread/events?after=0&limit=1000',
      { signal: controller.signal },
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/thread/events?after=1000&limit=1000',
      { signal: controller.signal },
    );
  });

  it('stops fetching further activity pages when the reader is cancelled', async () => {
    const first = Array.from({ length: 1000 }, (_, index) => ({ id: index + 1 }));
    fetchWithAuth.mockResolvedValue(jsonResponse({ events: first, next_cursor: 1000 }));
    const controller = new AbortController();
    const onPage = vi.fn(() => controller.abort());
    await expect(
      listAllAgentThreadEvents('job', { signal: controller.signal, onPage }),
    ).rejects.toMatchObject({ name: 'AbortError' });
    expect(fetchWithAuth).toHaveBeenCalledOnce();
    expect(onPage).toHaveBeenCalledOnce();
  });

  it('does not publish an in-flight page after cancellation even if transport ignores abort', async () => {
    let answer!: (response: Response) => void;
    fetchWithAuth.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
    const controller = new AbortController();
    const onPage = vi.fn();
    const pending = listAllAgentThreadEvents('job', { signal: controller.signal, onPage });
    const outcome = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    controller.abort();
    answer(jsonResponse({ events: [{ id: 1 }], next_cursor: 1 }));
    await outcome;
    expect(onPage).not.toHaveBeenCalled();
    expect(fetchWithAuth).toHaveBeenCalledOnce();
  });

  it('does not fetch history for an already cancelled reader', async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(
      listAllAgentThreadEvents('job', { signal: controller.signal }),
    ).rejects.toMatchObject({ name: 'AbortError' });
    expect(fetchWithAuth).not.toHaveBeenCalled();
  });

  it('archives and restores a whole task conversation', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'athr_1', archived: true, archived_at: '2026-07-29T12:00:00Z' }),
    );

    await archiveAgentJob('job/one');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/archive',
      { method: 'POST' },
    );

    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'athr_1', archived: false, archived_at: null }),
    );
    await restoreAgentJob('job/one');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/archive',
      { method: 'DELETE' },
    );
  });

  it('pins and unpins a whole task conversation', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'athr_1', pinned: true, pinned_at: '2026-07-29T12:00:00Z' }),
    );

    await pinAgentJob('job/one');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/pin',
      { method: 'POST' },
    );

    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'athr_1', pinned: false, pinned_at: null }),
    );
    await unpinAgentJob('job/one');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/pin',
      { method: 'DELETE' },
    );
  });

  it('forks a conversation from a settled turn', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ id: 'ajob_fork', state: 'succeeded' }));

    const fork = await forkAgentJob('job/one');

    expect(fork.id).toBe('ajob_fork');
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/fork',
      {
        method: 'POST',
        headers: { 'Idempotency-Key': expect.any(String) },
      },
    );
  });

  it('keeps the fork idempotency key while an ambiguous request is retryable', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        jsonResponse(
          {
            detail: {
              error: { type: 'sandbox_host_unavailable', message: 'host unavailable' },
            },
          },
          503,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ id: 'ajob_recovered', state: 'succeeded' }));

    await expect(forkAgentJob('job/retryable')).rejects.toThrow();
    await expect(forkAgentJob('job/retryable')).resolves.toMatchObject({ id: 'ajob_recovered' });

    const firstHeaders = fetchWithAuth.mock.calls[0][2]?.headers as Record<string, string>;
    const secondHeaders = fetchWithAuth.mock.calls[1][2]?.headers as Record<string, string>;
    expect(secondHeaders['Idempotency-Key']).toBe(firstHeaders['Idempotency-Key']);
  });

  it('keeps side chat recovery separate from another fork of the same anchor', async () => {
    fetchWithAuth
      .mockRejectedValueOnce(new Error('Response lost'))
      .mockResolvedValueOnce(jsonResponse({ id: 'manual-fork', state: 'succeeded' }))
      .mockResolvedValueOnce(jsonResponse({ id: 'side-recovered', state: 'succeeded' }));
    const options = { requestScope: 'side-chat:user:main-thread' };
    await expect(forkAgentJob('shared-anchor', options)).rejects.toThrow('Response lost');
    await forkAgentJob('shared-anchor');
    await expect(forkAgentJob('shared-anchor', options)).resolves.toMatchObject({
      id: 'side-recovered',
    });
    const headers = fetchWithAuth.mock.calls.map(
      (call: Parameters<typeof client.fetchWithAuth>) => call[2]?.headers as Record<string, string>,
    );
    expect(headers[0]['Idempotency-Key']).not.toBe(headers[1]['Idempotency-Key']);
    expect(headers[2]['Idempotency-Key']).toBe(headers[0]['Idempotency-Key']);
  });

  it('rotates the fork idempotency key after a durably failed saga', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        jsonResponse(
          {
            detail: {
              error: { type: 'native_fork_failed', message: 'reconciled as failed' },
            },
          },
          409,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ id: 'ajob_retried', state: 'succeeded' }));

    await expect(forkAgentJob('job/failed')).rejects.toThrow();
    await expect(forkAgentJob('job/failed')).resolves.toMatchObject({ id: 'ajob_retried' });

    const firstHeaders = fetchWithAuth.mock.calls[0][2]?.headers as Record<string, string>;
    const secondHeaders = fetchWithAuth.mock.calls[1][2]?.headers as Record<string, string>;
    expect(secondHeaders['Idempotency-Key']).not.toBe(firstHeaders['Idempotency-Key']);
  });

  it('leaves an explicit fork operation ID under the caller’s control after any receipt', async () => {
    const options = { requestId: 'persisted-side-chat-operation' };
    fetchWithAuth
      .mockResolvedValueOnce(jsonResponse({ id: 'committed-child' }))
      .mockResolvedValueOnce(
        jsonResponse({ detail: { error: { type: 'native_fork_failed', message: 'Failed' } } }, 409),
      )
      .mockResolvedValueOnce(jsonResponse({ id: 'committed-child' }));
    await forkAgentJob('anchor', options);
    await expect(forkAgentJob('anchor', options)).rejects.toThrow('Failed');
    await forkAgentJob('anchor', options);
    expect(
      fetchWithAuth.mock.calls.map(
        (call: Parameters<typeof client.fetchWithAuth>) => call[2]?.headers,
      ),
    ).toEqual([
      { 'Idempotency-Key': options.requestId },
      { 'Idempotency-Key': options.requestId },
      { 'Idempotency-Key': options.requestId },
    ]);
  });

  it('loads source-control integrations', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ providers: [{ provider: 'github', configured: true, connected: false }] }),
    );

    const result = await getAgentIntegrations();

    expect(result.providers[0].provider).toBe('github');
    expect(fetchWithAuth).toHaveBeenCalledWith(expect.any(String), '/v1/agent/integrations');
  });

  it.each([
    ['github', connectGitHub],
    ['gitlab', connectGitLab],
  ] as const)('completes a %s OAuth connection with its state', async (provider, connect) => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ provider, connected: true }));

    await connect('oauth-code', 'signed-state');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      `/v1/agent/integrations/${provider}/connect`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ code: 'oauth-code', state: 'signed-state' }),
      }),
    );
  });

  it('disconnects one source-control account', async () => {
    fetchWithAuth.mockResolvedValue(new Response(null, { status: 204 }));

    await disconnectAgentIntegration('gitlab', 'group/id');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/integrations/gitlab/connections/group%2Fid',
      { method: 'DELETE' },
    );
  });

  it('browses a job workspace with an encoded relative path', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ path: 'src/a file.ts', kind: 'file', content: 'x', size: 1 }),
    );

    await getAgentJobFiles('job/one', 'src/a file.ts');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/files?path=src%2Fa%20file.ts',
    );
  });

  it('writes a live workspace file with an encoded path', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ path: 'src/a file.ts', kind: 'file', content: 'updated' }),
    );

    await writeAgentJobFile('job/one', 'src/a file.ts', 'updated');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/files?path=src%2Fa%20file.ts',
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ content: 'updated' }) }),
    );
  });

  it('can include the sandbox manager session limit without changing the legacy list result', async () => {
    fetchWithAuth.mockResolvedValueOnce(jsonResponse({ terminals: [], max_sessions: 2 }));
    await expect(listAgentTerminals('job/one', { includeLimits: true })).resolves.toEqual({
      terminals: [],
      max_sessions: 2,
    });
  });

  it('manages PTY sessions and loads live Git state', async () => {
    const terminal = {
      id: 'term/1',
      shell: 'zsh',
      state: 'running',
      cwd: '/workspace',
      rows: 24,
      cols: 80,
      last_seq: 0,
    };
    fetchWithAuth
      .mockResolvedValueOnce(jsonResponse({ terminals: [terminal] }))
      .mockResolvedValueOnce(jsonResponse(terminal))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(
        jsonResponse({ available: true, branch: 'agent/x', changes: [], patch: '', commits: [] }),
      );

    await expect(listAgentTerminals('job/one')).resolves.toEqual([terminal]);
    await createAgentTerminal('job/one');
    await writeAgentTerminalInput('job/one', 'term/1', 'λ\r');
    await resizeAgentTerminal('job/one', 'term/1', 30, 100);
    await deleteAgentTerminal('job/one', 'term/1');
    await getAgentJobGit('ajob_1');

    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      1,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals',
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ rows: 24, cols: 80 }),
      }),
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      3,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals/term%2F1/input',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ data: 'zrsN' }) }),
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      4,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals/term%2F1/resize',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ rows: 30, cols: 100 }) }),
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      5,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals/term%2F1',
      { method: 'DELETE' },
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      6,
      expect.any(String),
      '/v1/agent/jobs/ajob_1/git',
    );
  });

  it('chunks large terminal pastes on complete UTF-8 characters', async () => {
    fetchWithAuth.mockResolvedValue(new Response(null, { status: 204 }));
    const pasted = `${'a'.repeat(65_535)}λ🙂z`;

    await writeAgentTerminalInput('job-1', 'term-1', pasted);

    expect(fetchWithAuth).toHaveBeenCalledTimes(2);
    const decoded = fetchWithAuth.mock.calls.map((call: unknown[]) => {
      const init = call[2] as RequestInit;
      const encoded = (JSON.parse(String(init.body)) as { data: string }).data;
      const bytes = Uint8Array.from(Buffer.from(encoded, 'base64'));
      expect(bytes.byteLength).toBeLessThanOrEqual(64 * 1024);
      return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    });
    expect(decoded.join('')).toBe(pasted);
  });

  it('uses a lower advertised input limit while preserving complete UTF-8 characters', async () => {
    fetchWithAuth.mockResolvedValue(new Response(null, { status: 204 }));
    const pasted = `${'a'.repeat(1023)}λ🙂${'z'.repeat(1024)}`;
    await writeAgentTerminalInput('job-1', 'term-1', pasted, 1024);
    const decoded = fetchWithAuth.mock.calls.map((call: unknown[]) => {
      const init = call[2] as RequestInit;
      const encoded = (JSON.parse(String(init.body)) as { data: string }).data;
      const bytes = Uint8Array.from(Buffer.from(encoded, 'base64'));
      expect(bytes.byteLength).toBeLessThanOrEqual(1024);
      return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    });
    expect(decoded.join('')).toBe(pasted);
    expect(fetchWithAuth).toHaveBeenCalledTimes(3);
  });

  it('streams terminal output and exit frames from an explicit resume cursor', async () => {
    fetchWithAuth.mockResolvedValue(
      sseResponse([
        'event: reset\ndata: {"seq":8,"reason":"output_truncated"}\n\n',
        'event: output\ndata: {"seq":9,"data":"aGk="}\n\n',
        'event: exit\ndata: {"seq":10,"exit_code":0}\n\n',
      ]),
    );
    const onOutput = vi.fn();
    const onReset = vi.fn();
    const onExit = vi.fn();

    await streamAgentTerminal('job/one', 'term/1', {
      after: 7,
      onOutput,
      onReset,
      onExit,
    });

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals/term%2F1/stream?after=7',
      { headers: { Accept: 'text/event-stream' }, signal: undefined },
    );
    expect(onReset).toHaveBeenCalledWith({ seq: 8, reason: 'output_truncated' });
    expect(onOutput).toHaveBeenCalledWith({ seq: 9, data: 'aGk=' });
    expect(onExit).toHaveBeenCalledWith({ seq: 10, exit_code: 0 });
  });

  it('resumes a dropped terminal stream without replaying output', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        sseResponse([
          'event: broken\ndata: {nope}\n\n',
          'event: output\ndata: {"seq":8,"data":"YQ=="}\n\n',
        ]),
      )
      .mockResolvedValueOnce(
        sseResponse([
          'event: output\ndata: {"seq":8,"data":"YQ=="}\n\n',
          'event: output\ndata: {"seq":9,"data":"Yg=="}\n\n',
          'event: exit\ndata: {"seq":10,"exit_code":0}\n\n',
        ]),
      );
    const onOutput = vi.fn();

    await streamAgentTerminal('job-1', 'term-1', {
      after: 7,
      onOutput,
      onReset: vi.fn(),
      onExit: vi.fn(),
    });

    expect(onOutput.mock.calls.map(([event]) => event.seq)).toEqual([8, 9]);
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      '/v1/agent/jobs/job-1/terminals/term-1/stream?after=8',
      expect.any(Object),
    );
  });

  it.each([429, 500, 503, 504])(
    'recovers from a terminal attach HTTP %i with backoff and the last output cursor',
    async (status) => {
      vi.useFakeTimers();
      try {
        fetchWithAuth
          .mockResolvedValueOnce(sseResponse(['event: output\ndata: {"seq":8,"data":"YQ=="}\n\n']))
          .mockResolvedValueOnce(jsonResponse({ detail: 'temporarily unavailable' }, status))
          .mockResolvedValueOnce(
            sseResponse([
              'event: output\ndata: {"seq":8,"data":"YQ=="}\n\n',
              'event: output\ndata: {"seq":9,"data":"Yg=="}\n\n',
              'event: exit\ndata: {"seq":10,"exit_code":0}\n\n',
            ]),
          );
        const onOutput = vi.fn();
        const onExit = vi.fn();
        const stream = streamAgentTerminal('job-1', 'term-1', {
          after: 7,
          onOutput,
          onReset: vi.fn(),
          onExit,
        });

        await vi.advanceTimersByTimeAsync(99);
        expect(fetchWithAuth).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(fetchWithAuth).toHaveBeenCalledTimes(2);
        await vi.advanceTimersByTimeAsync(199);
        expect(fetchWithAuth).toHaveBeenCalledTimes(2);
        await vi.advanceTimersByTimeAsync(1);
        await expect(stream).resolves.toBeUndefined();

        expect(fetchWithAuth).toHaveBeenCalledTimes(3);
        for (const request of fetchWithAuth.mock.calls.slice(1)) {
          expect(request[1]).toBe('/v1/agent/jobs/job-1/terminals/term-1/stream?after=8');
        }
        expect(onOutput.mock.calls.map(([event]) => event.seq)).toEqual([8, 9]);
        expect(onExit).toHaveBeenCalledOnce();
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it.each([400, 401, 403, 404, 409, 410, 422])(
    'does not retry a permanent terminal attach HTTP %i',
    async (status) => {
      vi.useFakeTimers();
      try {
        fetchWithAuth.mockResolvedValue(jsonResponse({ detail: 'terminal unavailable' }, status));

        await expect(
          streamAgentTerminal('job-1', 'term-1', {
            onOutput: vi.fn(),
            onReset: vi.fn(),
            onExit: vi.fn(),
          }),
        ).rejects.toMatchObject({ statusCode: status });

        expect(fetchWithAuth).toHaveBeenCalledOnce();
        expect(vi.getTimerCount()).toBe(0);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it.each([429, 503])('cleans up a pending terminal HTTP %i retry on abort', async (status) => {
    vi.useFakeTimers();
    try {
      fetchWithAuth.mockResolvedValue(jsonResponse({ detail: 'temporarily unavailable' }, status));
      const controller = new AbortController();
      const stream = streamAgentTerminal('job-1', 'term-1', {
        signal: controller.signal,
        onOutput: vi.fn(),
        onReset: vi.fn(),
        onExit: vi.fn(),
      });

      await vi.advanceTimersByTimeAsync(0);
      expect(vi.getTimerCount()).toBe(1);
      controller.abort();
      await expect(stream).resolves.toBeUndefined();
      expect(vi.getTimerCount()).toBe(0);
      await vi.advanceTimersByTimeAsync(5_000);
      expect(fetchWithAuth).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reports where history ends without moving the resume cursor', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        sseResponse([
          'event: attached\ndata: {"seq":9}\n\n',
          'event: output\ndata: {"seq":8,"data":"YQ=="}\n\n',
        ]),
      )
      .mockResolvedValueOnce(
        sseResponse([
          'event: attached\ndata: {"seq":12}\n\n',
          'event: output\ndata: {"seq":9,"data":"Yg=="}\n\n',
          'event: exit\ndata: {"seq":10,"exit_code":0}\n\n',
        ]),
      );
    const onAttached = vi.fn();
    const onOutput = vi.fn();

    await streamAgentTerminal('job-1', 'term-1', {
      after: 7,
      onAttached,
      onOutput,
      onReset: vi.fn(),
      onExit: vi.fn(),
    });

    expect(onAttached.mock.calls.map(([event]) => event.seq)).toEqual([9, 12]);
    expect(onOutput.mock.calls.map(([event]) => event.seq)).toEqual([8, 9]);
    // Resumed after the last output applied, not after the announced history.
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      '/v1/agent/jobs/job-1/terminals/term-1/stream?after=8',
      expect.any(Object),
    );
  });

  it('sends binary terminal input byte for byte', async () => {
    fetchWithAuth.mockResolvedValue(new Response(null, { status: 204 }));
    // An X10 mouse report: not valid UTF-8, so it must not be re-encoded.
    const report = Uint8Array.from([0x1b, 0x5b, 0x4d, 0x20, 0xa1, 0xbb]);

    await writeAgentTerminalInput('job-1', 'term-1', report);

    const init = fetchWithAuth.mock.calls[0][2] as RequestInit;
    const encoded = (JSON.parse(String(init.body)) as { data: string }).data;
    expect(Array.from(Buffer.from(encoded, 'base64'))).toEqual(Array.from(report));
  });

  it('splits long binary input that looks like UTF-8 continuation bytes', async () => {
    fetchWithAuth.mockResolvedValue(new Response(null, { status: 204 }));
    const bytes = new Uint8Array(10).fill(0x80);

    await writeAgentTerminalInput('job-1', 'term-1', bytes, 4);

    const sent = fetchWithAuth.mock.calls.map((call: unknown[]) => {
      const init = call[2] as RequestInit;
      const encoded = (JSON.parse(String(init.body)) as { data: string }).data;
      return Array.from(Buffer.from(encoded, 'base64'));
    });
    expect(sent.map((chunk: number[]) => chunk.length)).toEqual([4, 4, 2]);
    expect(sent.flat()).toEqual(Array.from(bytes));
  });

  it('advances the resume cursor when a truncated-output reset is received', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        sseResponse(['event: reset\ndata: {"seq":8,"reason":"output_truncated"}\n\n']),
      )
      .mockResolvedValueOnce(
        sseResponse([
          'event: output\ndata: {"seq":9,"data":"YQ=="}\n\n',
          'event: exit\ndata: {"seq":10,"exit_code":0}\n\n',
        ]),
      );
    const onReset = vi.fn();

    await streamAgentTerminal('job-1', 'term-1', {
      after: 7,
      onOutput: vi.fn(),
      onReset,
      onExit: vi.fn(),
    });

    expect(onReset).toHaveBeenCalledOnce();
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      '/v1/agent/jobs/job-1/terminals/term-1/stream?after=8',
      expect.any(Object),
    );
  });

  it('keeps reconnecting with capped backoff until it is aborted', async () => {
    vi.useFakeTimers();
    try {
      fetchWithAuth.mockRejectedValue(new Error('offline'));
      const controller = new AbortController();
      const stream = streamAgentTerminal('job-1', 'term-1', {
        signal: controller.signal,
        onOutput: vi.fn(),
        onReset: vi.fn(),
        onExit: vi.fn(),
      });

      for (let attempt = 0; attempt < 8; attempt += 1) {
        await vi.advanceTimersToNextTimerAsync();
      }
      expect(fetchWithAuth.mock.calls.length).toBeGreaterThan(7);
      controller.abort();
      await vi.runAllTimersAsync();
      await expect(stream).resolves.toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it('treats a missing thread as an old standalone job', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ detail: 'not found' }, 404));
    await expect(getAgentJobThread('ajob_old')).resolves.toBeNull();
  });

  it('sends one idempotency key per pending submission and drops it on success', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(jsonResponse({ id: 'ajob_new', state: 'queued' }, 201))
      .mockResolvedValueOnce(jsonResponse({ id: 'ajob_newer', state: 'queued' }, 201));

    await createAgentJob({ repo: 'owner/name', task_prompt: 'fix it', model: 'glm-5.1' });
    await createAgentJob({ repo: 'owner/name', task_prompt: 'fix it again', model: 'glm-5.1' });

    const first = fetchWithAuth.mock.calls[0][2]?.headers as Record<string, string>;
    const second = fetchWithAuth.mock.calls[1][2]?.headers as Record<string, string>;
    expect(first['Idempotency-Key']).toEqual(expect.any(String));
    expect(second['Idempotency-Key']).not.toBe(first['Idempotency-Key']);
  });

  it('trims surrounding whitespace from a task prompt before sending it', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ id: 'ajob_new', state: 'queued' }, 201));

    await createAgentJob({
      repo: 'owner/name',
      task_prompt: '\n  fix it\nthen test it  \n',
      model: 'glm-5.1',
    });

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs',
      expect.objectContaining({
        body: JSON.stringify({
          repo: 'owner/name',
          task_prompt: 'fix it\nthen test it',
          model: 'glm-5.1',
        }),
      }),
    );
  });

  it('delivers events and the terminal frame', async () => {
    fetchWithAuth.mockResolvedValue(
      sseResponse([frame(1, 'message', { text: 'a' }), frame(2, 'usage', { text: 'b' }), FINISHED]),
    );
    const events: string[] = [];
    const onFinished = vi.fn();
    await streamAgentJob('ajob_1', { onEvent: (e) => events.push(e.event_type), onFinished });
    expect(events).toEqual(['message', 'usage']);
    expect(onFinished).toHaveBeenCalledWith({ state: 'succeeded', published_pr_url: 'u' });
  });

  it('parses frames split across chunk boundaries', async () => {
    // The network decides where chunks end, not the frame format.
    const whole = frame(1, 'message', { text: 'split' }) + FINISHED;
    const cut = Math.floor(whole.length / 3);
    fetchWithAuth.mockResolvedValue(
      sseResponse([whole.slice(0, cut), whole.slice(cut, cut * 2), whole.slice(cut * 2)]),
    );
    const events: number[] = [];
    await streamAgentJob('ajob_1', { onEvent: (e) => events.push(e.id) });
    expect(events).toEqual([1]);
  });

  it('resumes from the last id after a mid-stream drop, with no gap or duplicate', async () => {
    // First connection ends without a terminal frame — exactly what the
    // server's stream cap produces for a long-running job.
    fetchWithAuth
      .mockResolvedValueOnce(sseResponse([frame(1, 'message', {}), frame(2, 'message', {})]))
      .mockResolvedValueOnce(sseResponse([frame(3, 'message', {}), FINISHED]));

    const seen: number[] = [];
    await streamAgentJob('ajob_1', { onEvent: (e) => seen.push(e.id) });

    expect(seen).toEqual([1, 2, 3]);
    const secondUrl = fetchWithAuth.mock.calls[1][1] as string;
    expect(secondUrl).toContain('after=2');
  });

  it('reports each connection the server accepts before its frames, and no refused one', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(sseResponse([frame(1, 'message', {})]))
      .mockResolvedValueOnce(sseResponse([frame(2, 'message', {}), FINISHED]))
      .mockResolvedValueOnce(jsonResponse({ detail: 'unavailable' }, 503));
    const seen: string[] = [];
    const record = {
      onOpen: () => seen.push('open'),
      onEvent: (e: { id: number }) => seen.push(`${e.id}`),
    };

    await streamAgentJob('ajob_1', record);
    expect(seen).toEqual(['open', '1', 'open', '2']);

    seen.length = 0;
    await streamAgentJob('ajob_1', { ...record, onError: () => seen.push('error') });
    expect(seen).toEqual(['error']);
  });

  it.each(['EOF', 'read error'])(
    'backs off repeatedly short job streams ending in %s',
    async (end) => {
      vi.useFakeTimers();
      try {
        const error = new Error('connection dropped');
        const responses: Response[] = [];
        fetchWithAuth.mockImplementation(async () => {
          let response: Response;
          if (end === 'EOF') {
            response = sseResponse([]);
          } else {
            response = new Response(
              new ReadableStream<Uint8Array>({
                start(controller) {
                  controller.error(error);
                },
              }),
            );
          }
          responses.push(response);
          return response;
        });
        const controller = new AbortController();
        const onError = vi.fn();
        const stream = streamAgentJob('ajob_1', {
          lastEventId: 42,
          signal: controller.signal,
          onEvent: vi.fn(),
          onError,
        });

        await vi.advanceTimersByTimeAsync(0);
        expect(fetchWithAuth).toHaveBeenCalledOnce();
        const delays = [100, 200, 400, 800, 1_600, 3_200, 5_000, 5_000];
        for (const [index, delay] of delays.entries()) {
          await vi.advanceTimersByTimeAsync(delay - 1);
          expect(fetchWithAuth).toHaveBeenCalledTimes(index + 1);
          await vi.advanceTimersByTimeAsync(1);
          expect(fetchWithAuth).toHaveBeenCalledTimes(index + 2);
        }

        expect(vi.getTimerCount()).toBe(1);
        controller.abort();
        await expect(stream).resolves.toBeUndefined();
        expect(vi.getTimerCount()).toBe(0);
        await vi.advanceTimersByTimeAsync(5_000);
        expect(fetchWithAuth).toHaveBeenCalledTimes(delays.length + 1);
        for (const request of fetchWithAuth.mock.calls) {
          expect(request[1]).toBe('/v1/agent/jobs/ajob_1/stream?after=42');
        }
        expect(responses.every((response) => !response.body?.locked)).toBe(true);
        if (end === 'read error') {
          expect(onError).toHaveBeenCalledTimes(delays.length + 1);
          expect(onError).toHaveBeenCalledWith(error);
        } else {
          expect(onError).not.toHaveBeenCalled();
        }
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it('resets job-stream backoff on event progress and keeps the resume cursor', async () => {
    vi.useFakeTimers();
    try {
      fetchWithAuth
        .mockResolvedValueOnce(sseResponse([]))
        .mockResolvedValueOnce(
          sseResponse([': keepalive\n\n', 'id: 11\nevent: message\ndata: {broken\n\n']),
        )
        .mockResolvedValueOnce(sseResponse([frame(11, 'message', {})]))
        .mockResolvedValueOnce(sseResponse([]))
        .mockResolvedValueOnce(sseResponse([FINISHED]));
      const onEvent = vi.fn();
      const onFinished = vi.fn();
      const stream = streamAgentJob('ajob_1', { lastEventId: 10, onEvent, onFinished });

      await vi.advanceTimersByTimeAsync(100);
      expect(fetchWithAuth).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(199);
      expect(fetchWithAuth).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1);
      expect(fetchWithAuth).toHaveBeenCalledTimes(3);
      expect(onEvent.mock.calls.map(([event]) => event.id)).toEqual([11]);
      await vi.advanceTimersByTimeAsync(99);
      expect(fetchWithAuth).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(1);
      expect(fetchWithAuth).toHaveBeenCalledTimes(4);
      await vi.advanceTimersByTimeAsync(199);
      expect(fetchWithAuth).toHaveBeenCalledTimes(4);
      await vi.advanceTimersByTimeAsync(1);
      await expect(stream).resolves.toBeUndefined();

      expect(fetchWithAuth.mock.calls.map((request: unknown[]) => request[1])).toEqual([
        '/v1/agent/jobs/ajob_1/stream?after=10',
        '/v1/agent/jobs/ajob_1/stream?after=10',
        '/v1/agent/jobs/ajob_1/stream?after=10',
        '/v1/agent/jobs/ajob_1/stream?after=11',
        '/v1/agent/jobs/ajob_1/stream?after=11',
      ]);
      expect(onFinished).toHaveBeenCalledOnce();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('starts from an explicit resume point', async () => {
    fetchWithAuth.mockResolvedValue(sseResponse([FINISHED]));
    await streamAgentJob('ajob_1', { onEvent: () => {}, lastEventId: 42 });
    expect(fetchWithAuth.mock.calls[0][1]).toContain('after=42');
  });

  it('ignores keepalive comment frames', async () => {
    fetchWithAuth.mockResolvedValue(
      sseResponse([': keepalive\n\n', frame(1, 'message', {}), FINISHED]),
    );
    const seen: number[] = [];
    await streamAgentJob('ajob_1', { onEvent: (e) => seen.push(e.id) });
    expect(seen).toEqual([1]);
  });

  it('drops an unparsable frame instead of ending the stream', async () => {
    // Losing one row beats losing the rest of the job.
    fetchWithAuth.mockResolvedValue(
      sseResponse(['id: 1\nevent: message\ndata: {not json\n\n', frame(2, 'usage', {}), FINISHED]),
    );
    const seen: string[] = [];
    await streamAgentJob('ajob_1', { onEvent: (e) => seen.push(e.event_type) });
    expect(seen).toEqual(['usage']);
  });

  it('stops immediately when aborted', async () => {
    const controller = new AbortController();
    controller.abort();
    await streamAgentJob('ajob_1', { onEvent: () => {}, signal: controller.signal });
    expect(fetchWithAuth).not.toHaveBeenCalled();
  });

  // The goal belongs to the conversation, not to a turn: a conversation whose
  // newest turn has been deleted still has one, so every route names the thread.
  it('reads and writes the goal through its conversation', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'thread/1', goal: null, revision: 4 }),
    );
    await expect(getAgentGoal('thread/1')).resolves.toEqual({
      thread_id: 'thread/1',
      goal: null,
      revision: 4,
    });
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/threads/thread%2F1/goal',
    );

    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'thread/1', goal: null, revision: 5 }),
    );
    await setAgentGoal('thread/1', {
      goal: { objective: 'ship it', max_rounds: 3 },
      revision: 4,
    });
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/threads/thread%2F1/goal',
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: { objective: 'ship it', max_rounds: 3 }, revision: 4 }),
      },
    );
  });

  it('pauses, resumes and clears without a revision, because those buttons are idempotent', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ thread_id: 'thread-1', revision: 6 }));
    await pauseAgentGoal('thread-1');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/threads/thread-1/goal/pause',
      { method: 'POST' },
    );
    fetchWithAuth.mockResolvedValue(jsonResponse({ thread_id: 'thread-1', revision: 7 }));
    await resumeAgentGoal('thread-1');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/threads/thread-1/goal/resume',
      { method: 'POST' },
    );
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'thread-1', cleared: true, revision: 8 }),
    );
    await expect(clearAgentGoal('thread-1')).resolves.toEqual({
      thread_id: 'thread-1',
      cleared: true,
      revision: 8,
    });
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/threads/thread-1/goal',
      { method: 'DELETE' },
    );
  });
});
