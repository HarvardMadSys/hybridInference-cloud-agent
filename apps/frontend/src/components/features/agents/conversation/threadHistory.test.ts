import { describe, expect, it, vi } from 'vitest';
import type { AgentJobApi, AgentThreadApi } from '@/lib/api/agents';
import type { AgentReadScope } from '@/lib/api/agentReadScope';
import { createThreadHistoryReader, mergeThreadHistory } from './threadHistory';

function page(turns: number[], next: string | null, sync = 'tail'): AgentThreadApi {
  return {
    thread_id: 'thread',
    next_cursor: next,
    sync_cursor: sync,
    jobs: turns.map((turn) => ({ id: `job-${turn}`, turn_no: turn, revision: 1 }) as AgentJobApi),
    messages: turns.flatMap((turn) => [
      {
        id: turn * 10 + 3,
        job_id: `job-${turn}`,
        role: 'user' as const,
        content: `prompt ${turn}`,
        created_at: null,
      },
      {
        id: turn * 10 + 1,
        job_id: `job-${turn}`,
        role: 'assistant' as const,
        content: `reply ${turn}`,
        created_at: null,
      },
      {
        id: turn * 10 + 2,
        job_id: `job-${turn}`,
        role: 'user' as const,
        content: `steer ${turn}`,
        mid_turn: true,
        created_at: null,
      },
    ]),
  };
}

function scope() {
  return {
    getAgentJob: vi.fn(),
    getAgentJobThread: vi.fn(),
    getUsage: vi.fn(),
    setUsage: vi.fn(),
    invalidate: vi.fn(),
  } satisfies AgentReadScope;
}

describe('bounded thread history cache', () => {
  it('loads one recent page, backfills only on request, and rereads only the mutable tail', async () => {
    const reads = scope();
    reads.getAgentJobThread.mockImplementation(async (_job, options) => {
      if (options?.anchor) return page([1999, 2000], 'older');
      if (options?.cursor === 'older') return page([1997, 1998], 'even-older');
      if (options?.after === 'tail') return page([1999, 2000], 'tail-2', 'updated-tail');
      if (options?.after === 'tail-2') return page([2001], null, 'updated-tail');
      throw new Error('Unexpected history read');
    });
    const reader = createThreadHistoryReader(reads);
    expect((await reader.read('job-2000'))?.jobs).toHaveLength(2);
    expect(reads.getAgentJobThread).toHaveBeenCalledTimes(1);
    expect((await reader.older('job-2000'))?.jobs).toHaveLength(4);
    const refreshed = await reader.read('job-2000');
    expect(refreshed?.jobs.map((job) => job.turn_no)).toEqual([1997, 1998, 1999, 2000, 2001]);
    expect(refreshed?.next_cursor).toBe('even-older');
    expect(reads.getAgentJobThread.mock.calls.slice(2).map((call) => call[1])).toEqual([
      { after: 'tail' },
      { after: 'tail-2' },
    ]);
    expect(new Set(refreshed?.messages.map((message) => message.id)).size).toBe(15);
  });

  it('keeps a distant navigation gap explicitly backfillable without eagerly reading it', async () => {
    const reads = scope();
    reads.getAgentJobThread
      .mockResolvedValueOnce(page([1, 2], null))
      .mockResolvedValueOnce(page([1999, 2000], 'gap'))
      .mockResolvedValueOnce(page([1997, 1998], 'more-gap'));
    const reader = createThreadHistoryReader(reads);
    await reader.read('job-2');
    const distant = await reader.read('job-2000');
    expect(distant?.next_cursor).toBe('gap');
    expect(distant?.jobs.map((job) => job.turn_no)).toEqual([1, 2, 1999, 2000]);
    expect(reads.getAgentJobThread).toHaveBeenCalledTimes(2);
    await reader.older('job-2000');
    expect(reads.getAgentJobThread).toHaveBeenLastCalledWith('job-2000', { cursor: 'gap' });
  });

  it('preserves prompt-first and mid-turn ordering across page boundaries and fuller previews', () => {
    const held = page([4], 'older');
    held.messages[1].content = 'complete reply';
    const incoming = page([3, 4], null);
    incoming.messages.find((message) => message.id === 41)!.content = 'preview';
    const merged = mergeThreadHistory(held, incoming, true);
    expect(merged.messages.map((message) => message.id)).toEqual([33, 31, 32, 43, 41, 42]);
    expect(merged.messages.find((message) => message.id === 41)?.content).toBe('complete reply');
    expect(merged.next_cursor).toBeNull();
  });

  it('keeps selected history and separately resolves the latest turn identity', async () => {
    const reads = scope();
    reads.getAgentJobThread.mockResolvedValue({
      ...page([2], 'older'),
      current_job_id: 'job-2000',
    });
    reads.getAgentJob.mockResolvedValue({ id: 'job-2000', turn_no: 2000 } as AgentJobApi);
    const found = await createThreadHistoryReader(reads).read('job-2');
    expect(found?.jobs.map((job) => job.turn_no)).toEqual([2, 2000]);
    expect(reads.getAgentJob).toHaveBeenCalledWith('job-2000');
  });

  it('hydrates a current-job preview before composer choices can use its clipped fields', async () => {
    const reads = scope();
    reads.getAgentJobThread.mockResolvedValue({
      ...page([2000], 'older'),
      current_job_id: 'job-2000',
      jobs: [{ id: 'job-2000', turn_no: 2000, thread_preview: true } as AgentJobApi],
    });
    const complete = {
      id: 'job-2000',
      turn_no: 2000,
      metadata: { complete: true },
    } as unknown as AgentJobApi;
    reads.getAgentJob.mockResolvedValue(complete);
    const found = await createThreadHistoryReader(reads).read('job-2000');
    expect(found?.jobs).toEqual([complete]);
    expect(reads.getAgentJob).toHaveBeenCalledWith('job-2000');
  });

  it('discards an older-page result overtaken by refresh and keeps its cursor retryable', async () => {
    const reads = scope();
    let finish!: (value: AgentThreadApi) => void;
    reads.getAgentJobThread
      .mockResolvedValueOnce(page([8], 'older'))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      )
      .mockResolvedValueOnce(page([8, 9], null));
    const reader = createThreadHistoryReader(reads);
    await reader.read('job-8');
    const pending = reader.older('job-8');
    await reader.read('job-8');
    finish(page([7], 'oldest'));
    expect(await pending).toBeNull();
    reads.getAgentJobThread.mockResolvedValueOnce(page([7], 'oldest'));
    expect((await reader.older('job-8'))?.next_cursor).toBe('oldest');
  });

  it('does not merge another conversation or owner scope and rejects looping cursors', async () => {
    const reads = scope();
    reads.getAgentJobThread
      .mockResolvedValueOnce(page([1], 'older'))
      .mockResolvedValueOnce({ ...page([99], null), thread_id: 'different' });
    const reader = createThreadHistoryReader(reads);
    await reader.read('job-1');
    expect((await reader.read('job-99'))?.messages).toHaveLength(3);
    const other = scope();
    other.getAgentJobThread.mockResolvedValue(page([5], 'tail'));
    const independent = createThreadHistoryReader(other);
    expect((await independent.read('job-5'))?.messages).toHaveLength(3);
    await expect(independent.read('job-5')).rejects.toThrow('did not advance');
  });
});
