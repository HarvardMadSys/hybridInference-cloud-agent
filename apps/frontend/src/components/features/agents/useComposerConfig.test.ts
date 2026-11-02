// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import { MODEL_CONNECTIONS_CHANGED } from './modelConnectionEvents';
import { useComposerConfig } from './useComposerConfig';
import { SOURCE_CONTROL_RECHECK_MS } from './sourceControlRecovery';

vi.mock('@/lib/api/agents', () => ({ getAgentConfig: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

/** A config told apart by its one repository. */
function snapshot(model: string): AgentConfigApi {
  return {
    repos: [model],
    runtimes: ['codex'],
    github_connected: false,
    github_install_url: null,
  };
}

function useScreen(retrySourceControl = false) {
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [error, setError] = useState<unknown>(null);
  const reading = useComposerConfig(
    setConfig,
    (cause) => {
      setConfig(null);
      setError(cause);
    },
    retrySourceControl,
  );
  return { config, error, ...reading };
}

beforeEach(() => {
  vi.mocked(getAgentConfig).mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it('a saved connection refresh supersedes the mount response, even when the mount finishes last', async () => {
  const old = deferred<AgentConfigApi>();
  const saved = deferred<AgentConfigApi>();
  vi.mocked(getAgentConfig).mockReturnValueOnce(old.promise).mockReturnValueOnce(saved.promise);
  const { result } = renderHook(useScreen);
  act(() => window.dispatchEvent(new Event(MODEL_CONNECTIONS_CHANGED)));
  await act(async () => saved.resolve(snapshot('new-model')));
  expect(result.current.config?.repos).toEqual(['new-model']);
  await act(async () => old.resolve(snapshot('removed-model')));
  expect(result.current.config?.repos).toEqual(['new-model']);
  expect(result.current.loading).toBe(false);
});

it('a late failure cannot erase a newer config from another tab', async () => {
  const old = deferred<AgentConfigApi>();
  vi.mocked(getAgentConfig)
    .mockReturnValueOnce(old.promise)
    .mockResolvedValue(snapshot('new-model'));
  const { result } = renderHook(useScreen);
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: MODEL_CONNECTIONS_CHANGED })));
  await waitFor(() => expect(result.current.config?.repos).toEqual(['new-model']));
  await act(async () => old.reject(new Error('old outage')));
  expect(result.current.error).toBeNull();
  expect(result.current.config?.repos).toEqual(['new-model']);
});

it('returning from provider setup refreshes choices and unmount fences a pending read', async () => {
  const next = deferred<AgentConfigApi>();
  vi.mocked(getAgentConfig).mockResolvedValueOnce(snapshot('first')).mockReturnValue(next.promise);
  const loaded = vi.fn();
  const { unmount } = renderHook(() => useComposerConfig(loaded, vi.fn()));
  await waitFor(() => expect(loaded).toHaveBeenCalledTimes(1));
  act(() => window.dispatchEvent(new Event('focus')));
  expect(getAgentConfig).toHaveBeenCalledTimes(2);
  unmount();
  await act(async () => next.resolve(snapshot('late')));
  expect(loaded).toHaveBeenCalledTimes(1);
});

it('waits for a slow recovery poll across timer ticks and applies its successful response', async () => {
  vi.useFakeTimers();
  const mount = deferred<AgentConfigApi>();
  const slow = deferred<AgentConfigApi>();
  const next = deferred<AgentConfigApi>();
  vi.mocked(getAgentConfig)
    .mockReturnValueOnce(mount.promise)
    .mockReturnValueOnce(slow.promise)
    .mockReturnValue(next.promise);
  const { result } = renderHook(() => useScreen(true));
  await act(async () => mount.resolve(snapshot('before-recovery')));
  await act(async () => vi.advanceTimersByTimeAsync(SOURCE_CONTROL_RECHECK_MS));
  expect(getAgentConfig).toHaveBeenCalledTimes(2);
  await act(async () => vi.advanceTimersByTimeAsync(SOURCE_CONTROL_RECHECK_MS * 3));
  expect(getAgentConfig).toHaveBeenCalledTimes(2);
  expect(result.current.refreshing).toBe(true);
  await act(async () => slow.resolve(snapshot('recovered')));
  expect(result.current.config?.repos).toEqual(['recovered']);
  expect(result.current.refreshing).toBe(false);
  await act(async () => vi.advanceTimersByTimeAsync(SOURCE_CONTROL_RECHECK_MS));
  expect(getAgentConfig).toHaveBeenCalledTimes(3);
});

it.each(['connection change', 'manual refresh'] as const)(
  '%s supersedes a pending recovery poll without letting its stale completion unblock another poll',
  async (trigger) => {
    vi.useFakeTimers();
    const mount = deferred<AgentConfigApi>();
    const slow = deferred<AgentConfigApi>();
    const changed = deferred<AgentConfigApi>();
    vi.mocked(getAgentConfig)
      .mockReturnValueOnce(mount.promise)
      .mockReturnValueOnce(slow.promise)
      .mockReturnValue(changed.promise);
    const { result } = renderHook(() => useScreen(true));
    await act(async () => mount.resolve(snapshot('before-change')));
    await act(async () => vi.advanceTimersByTimeAsync(SOURCE_CONTROL_RECHECK_MS));
    act(() => {
      if (trigger === 'connection change') {
        window.dispatchEvent(new Event(MODEL_CONNECTIONS_CHANGED));
      } else {
        void result.current.refresh({ fresh: true });
      }
    });
    expect(getAgentConfig).toHaveBeenCalledTimes(3);
    await act(async () => slow.resolve(snapshot('stale-model')));
    await act(async () => vi.advanceTimersByTimeAsync(SOURCE_CONTROL_RECHECK_MS * 2));
    expect(getAgentConfig).toHaveBeenCalledTimes(3);
    expect(result.current.config?.repos).toEqual(['before-change']);
    expect(result.current.refreshing).toBe(true);
    await act(async () => changed.resolve(snapshot('changed-model')));
    expect(result.current.config?.repos).toEqual(['changed-model']);
    expect(result.current.refreshing).toBe(false);
  },
);
