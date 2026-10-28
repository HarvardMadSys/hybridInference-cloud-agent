// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import { MODEL_CONNECTIONS_CHANGED } from './modelConnectionEvents';
import { useComposerConfig } from './useComposerConfig';

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

function snapshot(model: string): AgentConfigApi {
  return {
    repos: [],
    runtimes: ['codex'],
    models: [model],
    github_connected: false,
    github_install_url: null,
  };
}

function useScreen() {
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [error, setError] = useState<unknown>(null);
  const reading = useComposerConfig(setConfig, (cause) => {
    setConfig(null);
    setError(cause);
  });
  return { config, error, ...reading };
}

beforeEach(() => vi.mocked(getAgentConfig).mockReset());
afterEach(cleanup);

it('a saved connection refresh supersedes the mount response, even when the mount finishes last', async () => {
  const old = deferred<AgentConfigApi>();
  const saved = deferred<AgentConfigApi>();
  vi.mocked(getAgentConfig).mockReturnValueOnce(old.promise).mockReturnValueOnce(saved.promise);
  const { result } = renderHook(useScreen);
  act(() => window.dispatchEvent(new Event(MODEL_CONNECTIONS_CHANGED)));
  await act(async () => saved.resolve(snapshot('new-model')));
  expect(result.current.config?.models).toEqual(['new-model']);
  await act(async () => old.resolve(snapshot('removed-model')));
  expect(result.current.config?.models).toEqual(['new-model']);
  expect(result.current.loading).toBe(false);
});

it('a late failure cannot erase a newer config from another tab', async () => {
  const old = deferred<AgentConfigApi>();
  vi.mocked(getAgentConfig)
    .mockReturnValueOnce(old.promise)
    .mockResolvedValue(snapshot('new-model'));
  const { result } = renderHook(useScreen);
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: MODEL_CONNECTIONS_CHANGED })));
  await waitFor(() => expect(result.current.config?.models).toEqual(['new-model']));
  await act(async () => old.reject(new Error('old outage')));
  expect(result.current.error).toBeNull();
  expect(result.current.config?.models).toEqual(['new-model']);
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
