// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getMcpThreadBindings, updateMcpThreadBinding, type McpBindingApi } from '@/lib/api/mcp';
import { useThreadComposerMcp } from './useThreadComposerMcp';

vi.mock('@/lib/api/mcp', () => ({
  getMcpThreadBindings: vi.fn(),
  updateMcpThreadBinding: vi.fn(),
}));

function binding(overrides: Partial<McpBindingApi> = {}): McpBindingApi {
  return {
    connection_id: 7,
    connection_label: 'Notes',
    alias: 'notes',
    enabled: false,
    selection_pending: false,
    available: true,
    error: null,
    capabilities: [],
    tools: { search: 'deny' },
    resources: {},
    prompts: {},
    config_revision: 1,
    authorization_epoch: 1,
    capability_revision: 1,
    policy_revision: 1,
    ...overrides,
  };
}

beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it('reenables an empty selection with ask-before-call defaults', async () => {
  vi.mocked(getMcpThreadBindings).mockResolvedValue({ bindings: [binding()] });
  vi.mocked(updateMcpThreadBinding).mockResolvedValue({
    bindings: [binding({ enabled: true, selection_pending: true, tools: {} })],
  });
  const { result } = renderHook(() => useThreadComposerMcp('thread'));
  await waitFor(() => expect(result.current.choices).toHaveLength(1));
  await act(async () => result.current.toggle(result.current.choices[0]!.connection, true));
  expect(updateMcpThreadBinding).toHaveBeenCalledWith('thread', {
    connection_id: 7,
    alias: 'notes',
    enabled: true,
    use_defaults: true,
    tools: {},
    resources: {},
    prompts: {},
  });
  expect(result.current.count).toBe(1);
});

it('keeps the saved selection after a failed update and lets the user retry', async () => {
  vi.mocked(getMcpThreadBindings).mockResolvedValue({
    bindings: [binding({ enabled: true, selection_pending: true })],
  });
  vi.mocked(updateMcpThreadBinding)
    .mockRejectedValueOnce(new Error('Save failed'))
    .mockResolvedValueOnce({ bindings: [binding()] });
  const { result } = renderHook(() => useThreadComposerMcp('thread'));
  await waitFor(() => expect(result.current.count).toBe(1));
  await act(async () => result.current.toggle(result.current.choices[0]!.connection, false));
  expect(result.current.error).toBe('Save failed');
  expect(result.current.count).toBe(1);
  expect(result.current.saving).toBe(false);
  await act(async () => result.current.toggle(result.current.choices[0]!.connection, false));
  expect(result.current.error).toBeNull();
  expect(result.current.count).toBe(0);
});

it('ignores an old conversation update after switching threads', async () => {
  vi.mocked(getMcpThreadBindings)
    .mockResolvedValueOnce({ bindings: [binding()] })
    .mockResolvedValueOnce({ bindings: [] });
  let finish!: (value: { bindings: McpBindingApi[] }) => void;
  vi.mocked(updateMcpThreadBinding).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  const { result, rerender } = renderHook(({ threadId }) => useThreadComposerMcp(threadId), {
    initialProps: { threadId: 'old-thread' },
  });
  await waitFor(() => expect(result.current.choices).toHaveLength(1));
  act(() => {
    void result.current.toggle(result.current.choices[0]!.connection, true);
  });
  rerender({ threadId: 'new-thread' });
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => finish({ bindings: [binding({ enabled: true })] }));
  expect(result.current.choices).toEqual([]);
  expect(result.current.saving).toBe(false);
});
