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
} from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getMcpThreadBindings, updateMcpThreadBinding, type McpBindingApi } from '@/lib/api/mcp';
import { useThreadComposerMcp } from './useThreadComposerMcp';
import { McpSubmenu } from './McpSubmenu';

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

function ThreadMenu() {
  const mcp = useThreadComposerMcp('thread');
  return (
    <McpSubmenu
      choices={mcp.choices}
      loading={mcp.loading}
      error={mcp.error}
      onRetry={mcp.reload}
      onToggle={(choice, on) => void mcp.toggle(choice.connection, on)}
    />
  );
}

it.each([true, false])(
  'keeps off/on reversible and preserves permissions when available=%s',
  async (available) => {
    const saved = binding({
      enabled: true,
      available,
      error: available ? null : 'This connection is turned off.',
      tools: { search: 'allow', delete: 'deny' },
      resources: { 'notes://index': 'confirm' },
      prompts: { summarize: 'confirm' },
    });
    vi.mocked(getMcpThreadBindings).mockResolvedValue({ bindings: [saved] });
    vi.mocked(updateMcpThreadBinding).mockImplementation(async (_threadId, request) => ({
      bindings: [{ ...saved, enabled: request.enabled }],
    }));
    render(<ThreadMenu />);
    const toggle = await screen.findByRole('switch', { name: 'Notes' });
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: 'Notes' })).not.toBeChecked();
      expect(screen.getByRole('switch', { name: 'Notes' })).toBeEnabled();
    });
    fireEvent.click(screen.getByRole('switch', { name: 'Notes' }));
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Notes' })).toBeChecked());
    expect(updateMcpThreadBinding).toHaveBeenLastCalledWith('thread', {
      connection_id: 7,
      alias: 'notes',
      enabled: true,
      use_defaults: false,
      tools: saved.tools,
      resources: saved.resources,
      prompts: saved.prompts,
    });
    if (!available) {
      // A conversation preference never turns the account connection back on.
      expect(screen.getByText('This connection is turned off.')).toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Manage connection' })).toHaveAttribute(
        'href',
        '/settings/plugins',
      );
    }
  },
);

it('offers connection recovery after reopening an unavailable, disabled conversation choice', async () => {
  vi.mocked(getMcpThreadBindings).mockResolvedValue({
    bindings: [binding({ available: false, error: 'This connection is turned off.' })],
  });
  render(<ThreadMenu />);
  expect(await screen.findByRole('switch', { name: 'Notes' })).toBeEnabled();
  expect(screen.getByRole('switch', { name: 'Notes' })).not.toBeChecked();
  expect(screen.getByRole('link', { name: 'Manage connection' })).toHaveAttribute(
    'href',
    '/settings/plugins',
  );
});

it('keeps an unavailable new-task choice visible with a recovery route', () => {
  const onToggle = vi.fn();
  render(
    <McpSubmenu
      choices={[
        {
          connection: { id: 7, label: 'Notes' },
          selected: false,
          toolCount: 0,
          busy: false,
          blocked: true,
          error: 'turned off',
        },
      ]}
      loading={false}
      error={null}
      onRetry={vi.fn()}
      onToggle={onToggle}
    />,
  );
  const toggle = screen.getByRole('switch', { name: 'Notes' });
  expect(toggle).toBeDisabled();
  fireEvent.click(toggle);
  expect(onToggle).not.toHaveBeenCalled();
  expect(screen.getByRole('link', { name: 'Manage connection' })).toHaveAttribute(
    'href',
    '/settings/plugins',
  );
});

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
