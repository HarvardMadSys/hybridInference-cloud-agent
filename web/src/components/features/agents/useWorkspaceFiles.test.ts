// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentJobFiles, type AgentJobFilesApi } from '@/lib/api/agents';
import { useWorkspaceFiles } from './useAgentJobs';

vi.mock('@/lib/api/agents', () => ({ getAgentJobFiles: vi.fn() }));

function deferred() {
  let resolve!: (value: AgentJobFilesApi) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<AgentJobFilesApi>((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function file(path: string, content = path): AgentJobFilesApi {
  return { path, kind: 'file', content, size: content.length, binary: false, truncated: false };
}

async function mountFiles(responses: Record<string, ReturnType<typeof deferred>>) {
  vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
    if (!path) return { path: '', kind: 'directory', entries: [] };
    const response = responses[path];
    if (!response) throw new Error(`Unexpected file request: ${path}`);
    return response.promise;
  });
  const hook = renderHook(() => useWorkspaceFiles('ajob_test', true));
  await waitFor(() => expect(hook.result.current.directories['']?.entries).toEqual([]));
  return hook;
}

beforeEach(() => vi.mocked(getAgentJobFiles).mockReset());
afterEach(cleanup);

describe('workspace preview response ordering', () => {
  it.each(['file', 'directory'] as const)(
    'keeps the newly selected file when an older %s response arrives last',
    async (kind) => {
      const a = deferred();
      const b = deferred();
      const { result } = await mountFiles({ 'a.ts': a, 'b.ts': b });
      act(() => result.current.openFile('a.ts'));
      act(() => result.current.openFile('b.ts'));
      await act(async () => b.resolve(file('b.ts', 'selected content')));
      await act(async () =>
        a.resolve(
          kind === 'file' ? file('a.ts', 'old content') : { path: 'a.ts', kind, entries: [] },
        ),
      );

      expect(result.current.selected).toBe('b.ts');
      expect(result.current.file).toEqual(file('b.ts', 'selected content'));
      expect(result.current.fileLoading).toBe(false);
    },
  );

  it('keeps loading the selected file when an older request finishes first', async () => {
    const a = deferred();
    const b = deferred();
    const { result } = await mountFiles({ 'a.ts': a, 'b.ts': b });
    act(() => result.current.openFile('a.ts'));
    act(() => result.current.openFile('b.ts'));
    await act(async () => a.resolve(file('a.ts')));

    expect(result.current.fileLoading).toBe(true);
    expect(result.current.file).toBeNull();
    await act(async () => b.resolve(file('b.ts')));
    expect(result.current.file).toEqual(file('b.ts'));
    expect(result.current.fileLoading).toBe(false);
  });

  it('ignores an older error after the selected file has loaded', async () => {
    const a = deferred();
    const b = deferred();
    const { result } = await mountFiles({ 'a.ts': a, 'b.ts': b });
    act(() => result.current.openFile('a.ts'));
    act(() => result.current.openFile('b.ts'));
    await act(async () => b.resolve(file('b.ts')));
    await act(async () => a.reject(new Error('a.ts unavailable')));

    expect(result.current.fileError).toBeNull();
    expect(result.current.file).toEqual(file('b.ts'));
  });

  it('clears loading and discards the pending response when the preview is closed', async () => {
    const a = deferred();
    const { result } = await mountFiles({ 'a.ts': a });
    act(() => result.current.openFile('a.ts'));
    act(() => result.current.closeFile());

    expect(result.current.fileLoading).toBe(false);
    await act(async () => a.resolve(file('a.ts')));
    expect(result.current.selected).toBeNull();
    expect(result.current.file).toBeNull();
    expect(result.current.fileError).toBeNull();
  });

  it('keeps the refreshed content when the old request for the same path arrives later', async () => {
    const old = deferred();
    const fresh = deferred();
    const responses = { 'a.ts': old };
    const { result } = await mountFiles(responses);
    act(() => result.current.openFile('a.ts'));
    responses['a.ts'] = fresh;
    act(() => result.current.reload());
    await act(async () => fresh.resolve(file('a.ts', 'fresh')));
    await act(async () => old.resolve(file('a.ts', 'stale')));

    expect(result.current.file).toEqual(file('a.ts', 'fresh'));
  });
});
