// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentJobFiles,
  type AgentJobDirectoryApi,
  type AgentJobFilesApi,
} from '@/lib/api/agents';
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

function directory(path: string, names: string[]): AgentJobDirectoryApi {
  return {
    path,
    kind: 'directory',
    entries: names.map((name) => ({
      path: `${path}/${name}`,
      name,
      kind: 'file',
      size: 1,
    })),
  };
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

describe('workspace refresh', () => {
  it.each(['reload', 'refresh key'] as const)(
    'refetches a collapsed directory when reopened after a %s',
    async (refresh) => {
      const cached = directory('src', ['old.ts']);
      const fresh = directory('src', ['new.ts']);
      const pending = deferred();
      vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') =>
        path === 'src' ? cached : { path, kind: 'directory', entries: [] },
      );
      const hook = renderHook(({ key }) => useWorkspaceFiles('ajob_test', true, key), {
        initialProps: { key: 'running:0' },
      });
      await act(async () => hook.result.current.toggleDirectory('src'));
      act(() => hook.result.current.toggleDirectory('src'));
      vi.mocked(getAgentJobFiles).mockClear();
      vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') =>
        path === 'src' ? pending.promise : { path, kind: 'directory', entries: [] },
      );

      await act(async () => {
        if (refresh === 'reload') hook.result.current.reload();
        else hook.rerender({ key: 'running:1' });
      });
      expect(vi.mocked(getAgentJobFiles).mock.calls.map(([, path]) => path)).toEqual(['']);

      act(() => hook.result.current.toggleDirectory('src'));
      expect(hook.result.current.directories.src.entries).toEqual(cached.entries);
      expect(hook.result.current.directories.src.loading).toBe(true);
      await act(async () => pending.resolve(fresh));
      expect(hook.result.current.directories.src.entries).toEqual(fresh.entries);
      expect(hook.result.current.directories.src.loading).toBe(false);
    },
  );

  it('reuses a cached directory when reopened without a refresh', async () => {
    const cached = directory('src', ['a.ts']);
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') =>
      path === 'src' ? cached : { path, kind: 'directory', entries: [] },
    );
    const { result } = renderHook(() => useWorkspaceFiles('ajob_test', true));
    await act(async () => result.current.toggleDirectory('src'));
    vi.mocked(getAgentJobFiles).mockClear();

    act(() => result.current.toggleDirectory('src'));
    act(() => result.current.toggleDirectory('src'));

    expect(result.current.directories.src.entries).toEqual(cached.entries);
    expect(vi.mocked(getAgentJobFiles)).not.toHaveBeenCalled();
  });

  it('ignores an older directory response and keeps the current request when reopened', async () => {
    const old = deferred();
    const fresh = deferred();
    const responses = { src: old };
    const { result } = await mountFiles(responses);
    act(() => result.current.toggleDirectory('src'));
    act(() => result.current.toggleDirectory('src'));
    responses.src = fresh;
    act(() => result.current.reload());
    act(() => result.current.toggleDirectory('src'));
    await act(async () => old.resolve(directory('src', ['old.ts'])));

    expect(result.current.directories.src.entries).toBeNull();
    expect(result.current.directories.src.loading).toBe(true);
    act(() => result.current.toggleDirectory('src'));
    act(() => result.current.toggleDirectory('src'));
    expect(
      vi.mocked(getAgentJobFiles).mock.calls.filter(([, path]) => path === 'src'),
    ).toHaveLength(2);

    const loaded = directory('src', ['new.ts']);
    await act(async () => fresh.resolve(loaded));
    expect(result.current.directories.src.entries).toEqual(loaded.entries);
    expect(result.current.directories.src.loading).toBe(false);
  });

  it('keeps the tree on screen while it reloads', async () => {
    // A refresh fires while the agent is writing — exactly when the user is
    // watching the panel. Clearing the directories first blanked and
    // repainted the whole tree for a round trip and bought nothing:
    // `loadDirectory` already keeps the entries it has while it reloads.
    const entry = { path: 'a.txt', name: 'a.txt', kind: 'file' as const, size: 1 };
    const loaded: AgentJobFilesApi = { path: '', kind: 'directory', entries: [entry] };
    vi.mocked(getAgentJobFiles).mockResolvedValue(loaded);

    const hook = renderHook(({ key }) => useWorkspaceFiles('ajob_test', true, key), {
      initialProps: { key: 'running:0' },
    });
    await waitFor(() => expect(hook.result.current.directories['']?.entries).toEqual([entry]));

    const pending = deferred();
    vi.mocked(getAgentJobFiles).mockReturnValue(pending.promise);
    hook.rerender({ key: 'running:1' });

    await waitFor(() => expect(hook.result.current.directories['']?.loading).toBe(true));
    expect(hook.result.current.directories['']?.entries).toEqual([entry]);

    pending.resolve(loaded);
    await waitFor(() => expect(hook.result.current.directories['']?.loading).toBe(false));
  });

  it('reloads every expanded directory and the open file, and nothing else', async () => {
    // The fan-out is the reason a refresh is expensive: one request per
    // expanded directory plus the open file, each its own round trip. This
    // pins the shape so a future change cannot quietly widen it.
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') =>
      path.endsWith('.txt') ? file(path) : { path, kind: 'directory' as const, entries: [] },
    );

    const hook = renderHook(({ key }) => useWorkspaceFiles('ajob_test', true, key), {
      initialProps: { key: 'running:0' },
    });
    await waitFor(() => expect(hook.result.current.directories['']?.entries).toEqual([]));
    await act(async () => hook.result.current.toggleDirectory('src'));
    await act(async () => hook.result.current.openFile('a.txt'));
    vi.mocked(getAgentJobFiles).mockClear();

    await act(async () => {
      hook.rerender({ key: 'running:1' });
    });

    await waitFor(() => expect(vi.mocked(getAgentJobFiles).mock.calls.length).toBe(3));
    const paths = vi
      .mocked(getAgentJobFiles)
      .mock.calls.map(([, path]) => path)
      .sort();
    expect(paths).toEqual(['', 'a.txt', 'src']);
  });
});

describe('workspace request retries', () => {
  it('retries only the failed folder and shares its pending request across rapid retries', async () => {
    const failed = deferred();
    const retry = deferred();
    const responses = { src: failed };
    const { result } = await mountFiles(responses);
    act(() => result.current.toggleDirectory('src'));
    await act(async () => failed.reject(new Error('Folder unavailable')));
    expect(result.current.directories.src.error).toBe('Folder unavailable');
    vi.mocked(getAgentJobFiles).mockClear();
    responses.src = retry;

    act(() => {
      result.current.retryDirectory('src');
      result.current.retryDirectory('src');
      result.current.retryDirectory('src');
    });

    expect(vi.mocked(getAgentJobFiles).mock.calls.map(([, path]) => path)).toEqual(['src']);
    expect(result.current.directories.src.loading).toBe(true);
    expect(result.current.directories.src.error).toBeNull();
    const loaded = directory('src', ['recovered.ts']);
    await act(async () => retry.resolve(loaded));
    expect(result.current.directories.src).toEqual({
      entries: loaded.entries,
      loading: false,
      error: null,
    });
  });

  it.each(['response', 'error'] as const)(
    'ignores an old folder %s without releasing a newer pending retry',
    async (outcome) => {
      const old = deferred();
      const retry = deferred();
      const responses = { src: old };
      const { result } = await mountFiles(responses);
      act(() => result.current.toggleDirectory('src'));
      act(() => result.current.toggleDirectory('src'));
      act(() => result.current.reload());
      responses.src = retry;
      act(() => result.current.retryDirectory('src'));
      vi.mocked(getAgentJobFiles).mockClear();

      await act(async () => {
        if (outcome === 'response') old.resolve(directory('src', ['stale.ts']));
        else old.reject(new Error('Stale folder error'));
      });

      expect(result.current.directories.src).toEqual({
        entries: null,
        loading: true,
        error: null,
      });
      act(() => result.current.retryDirectory('src'));
      expect(getAgentJobFiles).not.toHaveBeenCalled();
      const loaded = directory('src', ['fresh.ts']);
      await act(async () => retry.resolve(loaded));
      expect(result.current.directories.src).toEqual({
        entries: loaded.entries,
        loading: false,
        error: null,
      });
    },
  );

  it.each(['response', 'error'] as const)(
    'keeps recovered folder contents when a previous-generation %s arrives last',
    async (outcome) => {
      const old = deferred();
      const retry = deferred();
      const responses = { src: old };
      const { result } = await mountFiles(responses);
      act(() => result.current.toggleDirectory('src'));
      act(() => result.current.toggleDirectory('src'));
      act(() => result.current.reload());
      responses.src = retry;
      act(() => result.current.retryDirectory('src'));
      const loaded = directory('src', ['fresh.ts']);
      await act(async () => retry.resolve(loaded));

      await act(async () => {
        if (outcome === 'response') old.resolve(directory('src', ['stale.ts']));
        else old.reject(new Error('Stale folder error'));
      });

      expect(result.current.directories.src).toEqual({
        entries: loaded.entries,
        loading: false,
        error: null,
      });
    },
  );

  it.each(['response', 'error'] as const)(
    'keeps the newly selected file after an earlier preview retry returns a %s',
    async (outcome) => {
      const failed = deferred();
      const retry = deferred();
      const selected = deferred();
      const responses = { 'a.ts': failed, 'b.ts': selected };
      const { result } = await mountFiles(responses);
      act(() => result.current.openFile('a.ts'));
      await act(async () => failed.reject(new Error('File unavailable')));
      expect(result.current.fileError).toBe('File unavailable');
      responses['a.ts'] = retry;
      act(() => result.current.retryFile());
      expect(result.current.fileError).toBeNull();
      expect(result.current.fileLoading).toBe(true);
      act(() => result.current.openFile('b.ts'));
      const chosen = file('b.ts', 'Selected content');
      await act(async () => selected.resolve(chosen));

      await act(async () => {
        if (outcome === 'response') retry.resolve(file('a.ts', 'Old retried content'));
        else retry.reject(new Error('Old retry error'));
      });

      expect(result.current.selected).toBe('b.ts');
      expect(result.current.file).toEqual(chosen);
      expect(result.current.fileError).toBeNull();
      expect(result.current.fileLoading).toBe(false);
    },
  );
});

describe('direct workspace folder navigation', () => {
  it('keeps a loaded preview when root navigation cancels its pending refresh', async () => {
    const folder = deferred();
    const first = deferred();
    const refreshed = deferred();
    const responses = { src: folder, 'a.ts': first };
    const { result } = await mountFiles(responses);
    act(() => result.current.openFile('src'));
    await act(async () => folder.resolve(directory('src', ['a.ts'])));
    act(() => result.current.openFile('a.ts'));
    const preview = file('a.ts', 'Loaded preview');
    await act(async () => first.resolve(preview));
    responses['a.ts'] = refreshed;
    act(() => result.current.reload());
    expect(result.current.fileLoading).toBe(true);

    act(() => result.current.showRootDirectory());

    expect(result.current.selected).toBe('a.ts');
    expect(result.current.file).toEqual(preview);
    expect(result.current.fileLoading).toBe(false);
    await act(async () => refreshed.resolve(directory('a.ts', ['late.txt'])));
    expect(result.current.directoryPath).toBeNull();
    expect(result.current.selected).toBe('a.ts');
    expect(result.current.file).toEqual(preview);
  });

  it('reveals a nested folder and loads the parent needed to reach its contents', async () => {
    const parent = deferred();
    const nested = deferred();
    const root: AgentJobDirectoryApi = {
      path: '',
      kind: 'directory',
      entries: [{ path: 'src', name: 'src', kind: 'directory' }],
    };
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (path === '') return root;
      if (path === 'src') return parent.promise;
      if (path === 'src/nested') return nested.promise;
      throw new Error(`Unexpected file request: ${path}`);
    });
    const { result } = renderHook(() => useWorkspaceFiles('ajob_test', true));
    await waitFor(() => expect(result.current.directories['']?.entries).toEqual(root.entries));
    vi.mocked(getAgentJobFiles).mockClear();

    act(() => result.current.openFile('src/nested'));
    const contents = directory('src/nested', ['file.ts']);
    await act(async () => nested.resolve(contents));

    expect(result.current.selected).toBeNull();
    expect(result.current.file).toBeNull();
    expect(result.current.fileLoading).toBe(false);
    expect(result.current.expanded).toMatchObject({ src: true, 'src/nested': true });
    expect(result.current.directories['src/nested'].entries).toEqual(contents.entries);
    expect(result.current.directories.src.loading).toBe(true);
    expect(vi.mocked(getAgentJobFiles).mock.calls.map(([, path]) => path)).toEqual([
      'src/nested',
      'src',
    ]);

    const ancestor: AgentJobDirectoryApi = {
      path: 'src',
      kind: 'directory',
      entries: [{ path: 'src/nested', name: 'nested', kind: 'directory' }],
    };
    await act(async () => parent.resolve(ancestor));
    expect(result.current.directories[''].entries).toEqual(root.entries);
    expect(result.current.directories.src.entries).toEqual(ancestor.entries);
    expect(result.current.directories['src/nested'].entries).toEqual(contents.entries);
  });
});
