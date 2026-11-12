// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  archiveAdminSandbox,
  deleteAdminSandbox,
  getAdminSandbox,
  restoreAdminSandbox,
  stopAdminSandbox,
  type AdminSandbox,
  type AdminSandboxDetail,
} from '@/lib/api/adminSandboxes';
import { APIError } from '@/lib/utils/errors';

vi.mock('@/lib/api/adminSandboxes', () => ({
  archiveAdminSandbox: vi.fn(),
  deleteAdminSandbox: vi.fn(),
  getAdminSandbox: vi.fn(),
  restoreAdminSandbox: vi.fn(),
  stopAdminSandbox: vi.fn(),
}));

import { BatchSandboxManagement } from './BatchSandboxManagement';
import type { SandboxObservation } from './present';
import { chooseOption } from '@/components/ui/__tests__/select';

const observedAt = '2026-10-10T10:00:00Z';
function detail(id: string, overrides: Partial<AdminSandbox> = {}): AdminSandboxDetail {
  return {
    observed_at: observedAt,
    sandbox: {
      id,
      origin_thread_id: `thread-${id}`,
      title: `Session ${id}`,
      user_id: 'owner-one',
      repo: 'org/repo',
      host: 'host-one',
      state: 'running',
      container_id: `container-${id}`,
      container_name: id,
      generation: 4,
      runtime_id: 'codex',
      runtime_version: '1',
      execution_profile: 'trusted-v1',
      memory_limit: '4g',
      cpu_limit: '2',
      member_count: 2,
      active_attempt_count: 0,
      active_operation_count: 0,
      terminal_count: 0,
      terminal_count_observed_at: observedAt,
      created_at: observedAt,
      last_activity_at: observedAt,
      idle_deadline: null,
      updated_at: observedAt,
      host_last_heartbeat_at: observedAt,
      ...overrides,
    },
    members: [],
  };
}

function observation(id: string, overrides: Partial<AdminSandbox> = {}): SandboxObservation {
  return {
    sandbox: detail(id, overrides).sandbox,
    observedAtMs: 1_000,
    serverObservedAt: observedAt,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const onDenied = vi.fn();
const onChanged = vi.fn();
const onCompleted = vi.fn();
const onBusyChange = vi.fn();
function controls(selected: SandboxObservation[], nowMs = 1_000) {
  return (
    <BatchSandboxManagement
      selected={selected}
      nowMs={nowMs}
      onDenied={onDenied}
      onChanged={onChanged}
      onCompleted={onCompleted}
      onBusyChange={onBusyChange}
    />
  );
}
const ACTION_LABELS: Record<string, string> = {
  stop: 'Stop idle sandboxes',
  delete: 'Archive to S3 and delete',
  discard: 'Delete without archive',
  archive: 'Remove from inventory',
  restore: 'Restore to inventory',
};
function choose(action: string) {
  chooseOption(
    screen.getByRole('combobox', { name: 'Batch sandbox action' }),
    ACTION_LABELS[action],
  );
}
async function review(count: number) {
  fireEvent.click(screen.getByRole('button', { name: 'Review selected sandboxes' }));
  await waitFor(() =>
    expect(screen.getByRole('button', { name: `Apply to ${count} sandboxes` })).toBeEnabled(),
  );
}
function mutations() {
  return [archiveAdminSandbox, deleteAdminSandbox, restoreAdminSandbox, stopAdminSandbox];
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(performance, 'now').mockReturnValue(1_000);
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.mocked(getAdminSandbox).mockImplementation(async (id) => detail(id));
  vi.mocked(stopAdminSandbox).mockImplementation(async (id) => detail(id, { state: 'stopping' }));
  vi.mocked(deleteAdminSandbox).mockImplementation(async (id) =>
    detail(id, { deletion_state: 'pending' }),
  );
  vi.mocked(archiveAdminSandbox).mockImplementation(async (id) =>
    detail(id, { archived_at: observedAt }),
  );
  vi.mocked(restoreAdminSandbox).mockImplementation(async (id) => detail(id));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('reviewed batch sandbox changes', () => {
  it('uses newly reviewed fences, skips busy sandboxes, and retains partial failures without retrying', async () => {
    const latestActivity = '2026-10-10T10:00:01Z';
    vi.mocked(getAdminSandbox).mockImplementation(async (id) =>
      detail(id, {
        generation: 8,
        container_id: `latest-${id}`,
        last_activity_at: latestActivity,
        active_attempt_count: id === 'busy' ? 1 : 0,
      }),
    );
    vi.mocked(stopAdminSandbox).mockImplementation(async (id) => {
      if (id === 'failed') throw new APIError('CONFLICT', 'Sandbox activity changed', 409);
      return detail(id, { state: 'stopping' });
    });
    render(controls(['accepted', 'failed', 'busy'].map((id) => observation(id))));
    choose('stop');
    await review(2);
    expect(screen.getByText(/Wait for all active turns to finish/)).toBeInTheDocument();
    expect(mutations().every((operation) => vi.mocked(operation).mock.calls.length === 0)).toBe(
      true,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Apply to 2 sandboxes' }));
    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith(['accepted']));
    expect(window.confirm).toHaveBeenCalledOnce();
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/4 (member )?sessions/));
    for (const id of ['accepted', 'failed']) {
      expect(stopAdminSandbox).toHaveBeenCalledWith(
        id,
        { generation: 8, container_id: `latest-${id}`, last_activity_at: latestActivity },
        expect.any(AbortSignal),
      );
    }
    expect(stopAdminSandbox).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(/Sandbox activity changed/)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('1 of 3 sandboxes accepted');
    expect(
      within(screen.getByRole('list', { name: 'Sandbox batch results' })).getByText(/Skipped/i),
    ).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledOnce();
    expect(onBusyChange).toHaveBeenLastCalledWith(false);
  });

  it('performs no mutation when the single batch confirmation is cancelled', async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    render(controls([observation('one'), observation('two')]));
    await review(2);
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 2 sandboxes' }));

    expect(window.confirm).toHaveBeenCalledOnce();
    for (const operation of mutations()) expect(operation).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
    expect(onCompleted).not.toHaveBeenCalled();
  });

  it('requires another review once reviewed details expire', async () => {
    const selected = [observation('one')];
    const view = render(controls(selected));
    await review(1);
    view.rerender(controls(selected, 91_001));
    expect(screen.getByRole('button', { name: 'Apply to 0 sandboxes' })).toBeDisabled();
    expect(screen.getByText(/Refresh details before changing this sandbox/)).toBeInTheDocument();
    expect(window.confirm).not.toHaveBeenCalled();
    for (const operation of mutations()) expect(operation).not.toHaveBeenCalled();
  });

  it('rechecks review age after a long confirmation before submitting any mutation', async () => {
    render(controls([observation('one')]));
    await review(1);
    vi.mocked(window.confirm).mockImplementation(() => {
      vi.mocked(performance.now).mockReturnValue(91_001);
      return true;
    });
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 1 sandboxes' }));
    expect(await screen.findByRole('status')).toHaveTextContent('0 of 1 sandboxes accepted');
    for (const operation of mutations()) expect(operation).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
    expect(onCompleted).not.toHaveBeenCalled();
    expect(screen.getByRole('list', { name: 'Sandbox batch results' })).toHaveTextContent(
      'Refresh details before changing this sandbox',
    );
  });

  it('clears the previous review when the action or selection changes', async () => {
    const one = [observation('one')];
    const view = render(controls(one));
    await review(1);
    choose('stop');
    expect(screen.queryByRole('button', { name: /Apply to/ })).not.toBeInTheDocument();
    await review(1);
    view.rerender(controls([observation('two')]));
    expect(screen.queryByRole('button', { name: /Apply to/ })).not.toBeInTheDocument();
    for (const operation of mutations()) expect(operation).not.toHaveBeenCalled();
  });

  it('refuses a detail response for a different sandbox while allowing a valid reviewed row', async () => {
    vi.mocked(getAdminSandbox).mockImplementation(async (id) =>
      detail(id === 'wrong' ? 'another-sandbox' : id),
    );
    render(controls([observation('valid'), observation('wrong')]));
    await review(1);
    expect(screen.getByRole('list', { name: 'Sandbox batch review' })).toHaveTextContent(
      'Sandbox identity changed. Review it again.',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 1 sandboxes' }));
    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith(['valid']));
    expect(deleteAdminSandbox).toHaveBeenCalledOnce();
    expect(vi.mocked(deleteAdminSandbox).mock.calls[0][0]).toBe('valid');
    expect(getAdminSandbox).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['delete', false],
    ['discard', true],
  ] as const)(
    'submits exact delete flags for %s and a reviewed running container',
    async (action, discard) => {
      render(controls([observation('one')]));
      choose(action);
      await review(1);
      fireEvent.click(screen.getByRole('button', { name: 'Apply to 1 sandboxes' }));
      await waitFor(() => expect(onCompleted).toHaveBeenCalledWith(['one']));
      expect(deleteAdminSandbox).toHaveBeenCalledWith(
        'one',
        { generation: 4, container_id: 'container-one', last_activity_at: observedAt },
        expect.any(AbortSignal),
        true,
        discard,
      );
      expect(window.confirm).toHaveBeenCalledOnce();
      if (discard)
        expect(window.confirm).toHaveBeenCalledWith(
          expect.stringContaining('without uploading an S3 archive'),
        );
      expect(stopAdminSandbox).not.toHaveBeenCalled();
    },
  );

  it('removes stopped and stale entries from inventory using the appropriate fence and stale flag', async () => {
    vi.mocked(getAdminSandbox).mockImplementation(async (id) =>
      id === 'stale'
        ? detail(id, { host_last_heartbeat_at: null })
        : detail(id, { state: 'stopped', container_id: null, terminal_count: null }),
    );
    render(controls([observation('stopped'), observation('stale')]));
    choose('archive');
    await review(2);
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 2 sandboxes' }));
    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith(['stopped', 'stale']));
    expect(archiveAdminSandbox).toHaveBeenCalledWith(
      'stopped',
      { generation: 4, container_id: null, last_activity_at: observedAt },
      expect.any(AbortSignal),
    );
    expect(archiveAdminSandbox).toHaveBeenCalledWith(
      'stale',
      { generation: 4, container_id: 'container-stale', last_activity_at: observedAt },
      expect.any(AbortSignal),
      true,
    );
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it('restores only reviewed entries already removed from inventory', async () => {
    vi.mocked(getAdminSandbox).mockImplementation(async (id) =>
      detail(id, { state: 'stopped', archived_at: id === 'removed' ? observedAt : null }),
    );
    render(controls([observation('removed'), observation('present')]));
    choose('restore');
    await review(1);
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 1 sandboxes' }));
    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith(['removed']));
    expect(restoreAdminSandbox).toHaveBeenCalledOnce();
    expect(restoreAdminSandbox).toHaveBeenCalledWith(
      'removed',
      { generation: 4, container_id: 'container-removed', last_activity_at: observedAt },
      expect.any(AbortSignal),
    );
  });

  it('bounds detail review concurrency and aborts outstanding reads on unmount', async () => {
    const pending = Array.from({ length: 6 }, () => deferred<AdminSandboxDetail>());
    vi.mocked(getAdminSandbox).mockImplementation((id) => pending[Number(id)].promise);
    const view = render(controls(pending.map((_, index) => observation(String(index)))));
    fireEvent.click(screen.getByRole('button', { name: 'Review selected sandboxes' }));
    await waitFor(() => expect(getAdminSandbox).toHaveBeenCalledTimes(4));
    expect(screen.getByRole('combobox', { name: 'Batch sandbox action' })).toBeDisabled();
    await act(async () => pending[0].resolve(detail('0')));
    await waitFor(() => expect(getAdminSandbox).toHaveBeenCalledTimes(5));
    view.unmount();
    for (const [, signal] of vi.mocked(getAdminSandbox).mock.calls)
      expect(signal?.aborted).toBe(true);
    await act(async () =>
      pending.slice(1).forEach((entry, index) => entry.resolve(detail(String(index + 1)))),
    );
    expect(getAdminSandbox).toHaveBeenCalledTimes(5);
    expect(onChanged).not.toHaveBeenCalled();
    expect(onDenied).not.toHaveBeenCalled();
  });

  it('aborts review and stops queued reads after authorization is lost', async () => {
    const pending = Array.from({ length: 6 }, () => deferred<AdminSandboxDetail>());
    vi.mocked(getAdminSandbox).mockImplementation((id) => pending[Number(id)].promise);
    render(controls(pending.map((_, index) => observation(String(index)))));
    fireEvent.click(screen.getByRole('button', { name: 'Review selected sandboxes' }));
    await waitFor(() => expect(getAdminSandbox).toHaveBeenCalledTimes(4));
    await act(async () => pending[0].reject(new APIError('FORBIDDEN', 'no', 403)));
    await waitFor(() => expect(onDenied).toHaveBeenCalledOnce());
    for (const [, signal] of vi.mocked(getAdminSandbox).mock.calls)
      expect(signal?.aborted).toBe(true);
    await act(async () =>
      pending.slice(1).forEach((entry, index) => entry.resolve(detail(String(index + 1)))),
    );
    expect(getAdminSandbox).toHaveBeenCalledTimes(4);
    for (const operation of mutations()) expect(operation).not.toHaveBeenCalled();
  });

  it('aborts mutations and stops queued changes after authorization is lost', async () => {
    const pending = Array.from({ length: 6 }, () => deferred<AdminSandboxDetail>());
    vi.mocked(deleteAdminSandbox).mockImplementation((id) => pending[Number(id)].promise);
    render(controls(pending.map((_, index) => observation(String(index)))));
    await review(6);
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 6 sandboxes' }));
    await waitFor(() => expect(deleteAdminSandbox).toHaveBeenCalledTimes(4));
    await act(async () => pending[0].reject(new APIError('FORBIDDEN', 'no', 403)));
    await waitFor(() => expect(onDenied).toHaveBeenCalledOnce());
    for (const [, , signal] of vi.mocked(deleteAdminSandbox).mock.calls)
      expect(signal?.aborted).toBe(true);
    await act(async () =>
      pending.slice(1).forEach((entry, index) => entry.resolve(detail(String(index + 1)))),
    );
    expect(deleteAdminSandbox).toHaveBeenCalledTimes(4);
    expect(onChanged).not.toHaveBeenCalled();
    expect(onCompleted).not.toHaveBeenCalled();
  });

  it('aborts pending mutations on unmount and ignores their late results', async () => {
    const pending = deferred<AdminSandboxDetail>();
    vi.mocked(deleteAdminSandbox).mockReturnValue(pending.promise);
    const view = render(controls([observation('one')]));
    await review(1);
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 1 sandboxes' }));
    await waitFor(() => expect(deleteAdminSandbox).toHaveBeenCalledOnce());
    const signal = vi.mocked(deleteAdminSandbox).mock.calls[0][2];
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(detail('one', { deletion_state: 'pending' })));
    expect(onChanged).not.toHaveBeenCalled();
    expect(onCompleted).not.toHaveBeenCalled();
    expect(onDenied).not.toHaveBeenCalled();
  });
});

describe('cold sandboxes in a batch', () => {
  const longAgo = '2026-10-01T00:00:00Z';
  const cold = {
    gone: { state: 'cold', host_status: 'disabled', host_last_heartbeat_at: longAgo },
    reporting: { state: 'cold', host_status: 'online', host_last_heartbeat_at: observedAt },
  } as const;

  it('deletes cold sandboxes whose host is gone at once and says why the others were skipped', async () => {
    vi.mocked(getAdminSandbox).mockImplementation(async (id) =>
      detail(id, id === 'running' ? {} : id === 'gone' ? cold.gone : cold.reporting),
    );
    vi.mocked(deleteAdminSandbox).mockImplementation(async (id) =>
      id === 'gone'
        ? detail(id, { state: 'lost', deletion_state: 'deleted', deletion_discard: true })
        : detail(id, { deletion_state: 'pending', deletion_discard: true }),
    );
    render(controls(['gone', 'reporting', 'running'].map((id) => observation(id))));
    choose('discard');
    await review(2);
    const reviewList = screen.getByRole('list', { name: 'Sandbox batch review' });
    expect(reviewList).toHaveTextContent('the next message to one of its chats restores it');
    expect(reviewList).not.toHaveTextContent('clean stop');

    fireEvent.click(screen.getByRole('button', { name: 'Apply to 2 sandboxes' }));
    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith(['gone', 'running']));
    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining('1 other selected sandbox is skipped; the review lists why.'),
    );
    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining('1 of them exist only as a checkpoint in S3'),
    );
    expect(deleteAdminSandbox).toHaveBeenCalledWith(
      'gone',
      { generation: 4, container_id: 'container-gone', last_activity_at: observedAt },
      expect.any(AbortSignal),
      false,
      true,
    );
    expect(deleteAdminSandbox).not.toHaveBeenCalledWith(
      'reporting',
      expect.anything(),
      expect.anything(),
      expect.anything(),
      expect.anything(),
    );
    const results = screen.getByRole('list', { name: 'Sandbox batch results' });
    expect(results).toHaveTextContent(
      'completed — Deleted at once: only its checkpoint in S3 held it, so no host was asked.',
    );
    expect(results).toHaveTextContent('requested — Deletion requested. Waiting for the host.');
    expect(results).toHaveTextContent(/skipped — Only its checkpoint in S3 holds this sandbox/);
    expect(screen.getByRole('status')).toHaveTextContent(
      'Batch finished. 2 of 3 sandboxes accepted. 1 skipped; each says why below.',
    );
  });

  it('skips cold sandboxes when archiving, since there is no container to archive', async () => {
    vi.mocked(getAdminSandbox).mockImplementation(async (id) =>
      detail(id, id === 'gone' ? cold.gone : {}),
    );
    render(controls([observation('gone'), observation('running')]));
    choose('delete');
    await review(1);
    expect(screen.getByRole('list', { name: 'Sandbox batch review' })).toHaveTextContent(
      'so there is no container to archive. Use Delete without archive',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 1 sandboxes' }));
    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith(['running']));
    expect(deleteAdminSandbox).toHaveBeenCalledOnce();
  });

  it('reports every skipped sandbox even when none can be applied any more', async () => {
    render(controls([observation('one'), observation('two')]));
    await review(2);
    // The review ages between the last render and the click.
    vi.mocked(performance.now).mockReturnValue(91_001);
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 2 sandboxes' }));

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Batch finished. 0 of 2 sandboxes accepted. 2 skipped; each says why below.',
    );
    const results = screen.getByRole('list', { name: 'Sandbox batch results' });
    expect(
      within(results).getAllByText(/Refresh details before changing this sandbox/),
    ).toHaveLength(2);
    expect(window.confirm).not.toHaveBeenCalled();
    for (const operation of mutations()) expect(operation).not.toHaveBeenCalled();
  });
});
