import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getAgentJob, getAgentJobThread, type AgentJobApi, type AgentThreadApi } from './agents';
import { createAgentReadScope } from './agentReadScope';

vi.mock('./agents', () => ({ getAgentJob: vi.fn(), getAgentJobThread: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: Error) => void;
  const promise = new Promise<T>((done, failed) => {
    resolve = done;
    reject = failed;
  });
  return { promise, resolve, reject };
}

const JOB = { id: 'job-a' } as AgentJobApi;

beforeEach(() => vi.resetAllMocks());

describe('owner-scoped pending reads', () => {
  it('joins a job read while pending and asks again after settlement', async () => {
    const held = deferred<AgentJobApi>();
    vi.mocked(getAgentJob).mockReturnValueOnce(held.promise).mockResolvedValue(JOB);
    const reads = createAgentReadScope();
    const sidebar = reads.getAgentJob('job-a');
    const page = reads.getAgentJob('job-a');
    expect(getAgentJob).toHaveBeenCalledOnce();
    held.resolve(JOB);
    expect(await sidebar).toBe(JOB);
    expect(await page).toBe(JOB);
    await reads.getAgentJob('job-a');
    expect(getAgentJob).toHaveBeenCalledTimes(2);
  });

  it('joins a thread read without retaining even a missing-thread result', async () => {
    const held = deferred<AgentThreadApi | null>();
    vi.mocked(getAgentJobThread).mockReturnValueOnce(held.promise).mockResolvedValue(null);
    const reads = createAgentReadScope();
    const sidebar = reads.getAgentJobThread('job-a');
    const page = reads.getAgentJobThread('job-a');
    expect(getAgentJobThread).toHaveBeenCalledOnce();
    held.resolve(null);
    expect(await sidebar).toBeNull();
    expect(await page).toBeNull();
    await reads.getAgentJobThread('job-a');
    expect(getAgentJobThread).toHaveBeenCalledTimes(2);
  });

  it('lets both readers see an error and allows the next read to retry', async () => {
    const held = deferred<AgentJobApi>();
    vi.mocked(getAgentJob).mockReturnValueOnce(held.promise).mockResolvedValue(JOB);
    const reads = createAgentReadScope();
    const sidebar = expect(reads.getAgentJob('job-a')).rejects.toThrow('offline');
    const page = expect(reads.getAgentJob('job-a')).rejects.toThrow('offline');
    held.reject(new Error('offline'));
    await Promise.all([sidebar, page]);
    expect(await reads.getAgentJob('job-a')).toBe(JOB);
    expect(getAgentJob).toHaveBeenCalledTimes(2);
  });

  it('keeps distinct job ids and owner scopes independent', async () => {
    const held = deferred<AgentJobApi>();
    vi.mocked(getAgentJob).mockReturnValue(held.promise);
    const alice = createAgentReadScope();
    const bob = createAgentReadScope();
    alice.getAgentJob('job-a');
    alice.getAgentJob('job-b');
    bob.getAgentJob('job-a');
    expect(vi.mocked(getAgentJob).mock.calls).toEqual([['job-a'], ['job-b'], ['job-a']]);
    held.resolve(JOB);
    await held.promise;
  });

  it('starts a fresh read on invalidation and protects it from older cleanup', async () => {
    const old = deferred<AgentJobApi>();
    const fresh = deferred<AgentJobApi>();
    vi.mocked(getAgentJob).mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise);
    const reads = createAgentReadScope();
    const first = reads.getAgentJob('job-a');
    reads.invalidate('job-a');
    const second = reads.getAgentJob('job-a');
    expect(getAgentJob).toHaveBeenCalledTimes(2);
    old.resolve(JOB);
    await first;
    expect(reads.getAgentJob('job-a')).toBe(second);
    expect(getAgentJob).toHaveBeenCalledTimes(2);
    fresh.resolve(JOB);
    await second;
  });
});
