import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAdminSandboxes,
  type AdminSandbox,
  type AdminSandboxesPage,
} from '@/lib/api/adminSandboxes';

import { readInventory, type SandboxInventorySnapshot } from './inventorySync';

vi.mock('@/lib/api/adminSandboxes', () => ({ getAdminSandboxes: vi.fn() }));
const observed = '2026-10-22T00:00:00Z';
const signal = () => new AbortController().signal;
const row = (id: string) =>
  ({ id, host: 'host-one', host_last_heartbeat_at: observed }) as AdminSandbox;
const delta = (extra: Partial<AdminSandboxesPage> = {}): AdminSandboxesPage => ({
  mode: 'delta',
  sandboxes: [],
  total_count: null,
  next_cursor: null,
  observed_at: observed,
  change_cursor: 'next-change',
  ...extra,
});

function previous(count = 2): SandboxInventorySnapshot {
  return {
    sandboxes: Array.from({ length: count }, (_, index) => ({
      sandbox: row(`sandbox-${index}`),
      observedAtMs: 10,
      serverObservedAt: observed,
    })),
    observedAt: observed,
    appliedAtMs: 10,
    changeCursor: 'prior-change',
  };
}

beforeEach(() => vi.resetAllMocks());

describe('bounded inventory synchronization', () => {
  it.each([1, 1000])(
    'uses one small unchanged response after warming %i sandboxes',
    async (count) => {
      const rows = Array.from({ length: count }, (_, index) => row(`sandbox-${index}`));
      const pages = Math.ceil(count / 200);
      for (let index = 0; index < pages; index++)
        vi.mocked(getAdminSandboxes).mockResolvedValueOnce({
          mode: 'snapshot',
          sandboxes: rows.slice(index * 200, (index + 1) * 200),
          observed_at: observed,
          total_count: count,
          next_cursor: index + 1 < pages ? `page-${index + 1}` : null,
          change_cursor: index + 1 === pages ? 'initial-change' : null,
        });
      vi.mocked(getAdminSandboxes).mockResolvedValue(delta());
      const initial = await readInventory({ limit: 200 }, null, signal());
      expect(getAdminSandboxes).toHaveBeenCalledTimes(pages + 1);
      expect(initial.sandboxes).toHaveLength(count);
      vi.mocked(getAdminSandboxes).mockClear();
      const refreshed = await readInventory({ limit: 200 }, initial, signal());
      expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
      expect(getAdminSandboxes).toHaveBeenCalledWith(
        { limit: 200, sync: true, cursor: 'next-change' },
        expect.any(AbortSignal),
      );
      expect(JSON.stringify(delta()).length).toBeLessThan(300);
      expect(refreshed.sandboxes[0]).toBe(initial.sandboxes[0]);
    },
  );

  it('applies additions, filtered removals and host changes without resetting sample age', async () => {
    const initial = previous();
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      delta({
        sandboxes: [row('new')],
        deleted_ids: ['sandbox-1'],
        hosts: [{ host: 'host-one', last_heartbeat_at: '2026-10-22T00:00:30Z' }],
      }),
    );
    const updated = await readInventory({}, initial, signal());
    expect(updated.sandboxes.map((item) => item.sandbox.id)).toEqual(['sandbox-0', 'new']);
    expect(updated.sandboxes[0].sandbox.host_last_heartbeat_at).toBe('2026-10-22T00:00:30Z');
    expect(updated.sandboxes[0].observedAtMs).toBe(10);
    expect(initial.sandboxes[0].sandbox.host_last_heartbeat_at).toBe(observed);
  });

  it('resynchronizes an expired cursor once and does not retain missing old rows', async () => {
    vi.mocked(getAdminSandboxes)
      .mockResolvedValueOnce(delta({ reset_required: true }))
      .mockResolvedValueOnce({
        mode: 'snapshot',
        sandboxes: [row('replacement')],
        total_count: 1,
        observed_at: observed,
        next_cursor: null,
        change_cursor: 'fresh',
      })
      .mockResolvedValueOnce(delta());
    const result = await readInventory({}, previous(), signal());
    expect(result.sandboxes.map((item) => item.sandbox.id)).toEqual(['replacement']);
    expect(vi.mocked(getAdminSandboxes).mock.calls[1][0]).not.toHaveProperty('cursor');
    expect(getAdminSandboxes).toHaveBeenCalledTimes(3);
  });

  it('does not loop indefinitely if resynchronization also expires', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(delta({ reset_required: true }));
    await expect(readInventory({}, previous(), signal())).rejects.toThrow('complete sandbox list');
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
  });

  it('bounds a busy delta round to ten pages and resumes its continuation', async () => {
    for (let index = 0; index < 10; index++)
      vi.mocked(getAdminSandboxes).mockResolvedValueOnce(
        delta({ next_cursor: `delta-${index}`, change_cursor: null }),
      );
    const partial = await readInventory({}, previous(), signal());
    expect(getAdminSandboxes).toHaveBeenCalledTimes(10);
    expect(partial.changeCursor).toBe('delta-9');
    vi.mocked(getAdminSandboxes).mockResolvedValueOnce(delta());
    await readInventory({}, partial, signal());
    expect(getAdminSandboxes).toHaveBeenLastCalledWith(
      { sync: true, cursor: 'delta-9' },
      expect.any(AbortSignal),
    );
  });

  it('does not mutate the last complete snapshot when a later delta page fails', async () => {
    const initial = previous();
    vi.mocked(getAdminSandboxes)
      .mockResolvedValueOnce(
        delta({
          deleted_ids: ['sandbox-0'],
          next_cursor: 'later',
          change_cursor: null,
        }),
      )
      .mockRejectedValueOnce(new Error('unavailable'));
    await expect(readInventory({}, initial, signal())).rejects.toThrow('unavailable');
    expect(initial.sandboxes).toHaveLength(2);
    expect(initial.changeCursor).toBe('prior-change');
  });
});
