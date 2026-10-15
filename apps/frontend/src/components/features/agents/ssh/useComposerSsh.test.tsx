// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  getSshThreadBindings,
  listSshServers,
  replaceSshThreadBindings,
  type SshServerApi,
} from '@/lib/api/sshServers';
import { useComposerSsh, useThreadComposerSsh } from './useComposerSsh';

vi.mock('@/lib/api/sshServers', () => ({
  listSshServers: vi.fn(),
  getSshThreadBindings: vi.fn(),
  replaceSshThreadBindings: vi.fn(),
}));

function server(id: string, status: SshServerApi['status']): SshServerApi {
  return {
    id,
    name: id,
    username: 'u',
    address: `${id}.example.org`,
    pinned_address: status === 'ready' ? '93.184.216.34' : null,
    port: 22,
    status,
  } as SshServerApi;
}

describe('useComposerSsh', () => {
  afterEach(() => vi.clearAllMocks());

  it('reads nothing until asked and sends only ready servers', async () => {
    vi.mocked(listSshServers).mockResolvedValue({
      configured: true,
      servers: [server('gpu', 'ready'), server('lab', 'pending')],
    });
    const { result } = renderHook(() => useComposerSsh());
    expect(listSshServers).not.toHaveBeenCalled();

    act(() => result.current.reload());
    await waitFor(() => expect(result.current.choices).toHaveLength(2));
    act(() => {
      result.current.toggle('gpu', true);
      result.current.toggle('lab', true);
    });

    expect(result.current.choices.find((choice) => choice.id === 'lab')?.blocked).toBe(true);
    expect(result.current.serverIds).toEqual(['gpu']);
    expect(result.current.count).toBe(1);
  });
});

describe('useThreadComposerSsh', () => {
  afterEach(() => vi.clearAllMocks());

  it('replaces the whole selection on each switch and keeps an on server switchable', async () => {
    vi.mocked(listSshServers).mockResolvedValue({
      configured: true,
      servers: [server('gpu', 'ready'), server('lab', 'pending'), server('db', 'ready')],
    });
    vi.mocked(getSshThreadBindings).mockResolvedValue([
      { server_id: 'lab', name: 'lab', status: 'pending', enabled: true },
    ]);
    vi.mocked(replaceSshThreadBindings).mockResolvedValue([
      { server_id: 'lab', name: 'lab', status: 'pending', enabled: true },
      { server_id: 'gpu', name: 'gpu', status: 'ready', enabled: true },
    ]);
    const { result } = renderHook(() => useThreadComposerSsh('athr_1'));
    expect(getSshThreadBindings).not.toHaveBeenCalled();

    act(() => result.current.reload());
    await waitFor(() => expect(result.current.count).toBe(1));
    const lab = result.current.choices.find((choice) => choice.id === 'lab');
    expect(lab).toMatchObject({ selected: true, blocked: false });

    act(() => result.current.toggle('gpu', true));
    await waitFor(() => expect(result.current.count).toBe(2));
    expect(replaceSshThreadBindings).toHaveBeenCalledWith('athr_1', ['lab', 'gpu']);
  });
});
