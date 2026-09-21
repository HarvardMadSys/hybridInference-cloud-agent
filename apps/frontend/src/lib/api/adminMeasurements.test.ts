import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  getMeasurement,
  getMeasurementWriterStatus,
  getProfilingControl,
  listMeasurements,
  listProfilingRuns,
  measurementOperationsFor,
  startProfilingRun,
  stopProfilingRun,
  summarizeProfilingRun,
  type ProfilingControlSnapshot,
} from './adminMeasurements';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

function stub() {
  return vi
    .spyOn(client, 'fetchWithAuth')
    .mockImplementation(
      async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
    );
}

function urlOf(fetch: { mock: { calls: unknown[][] } }, call = 0): URL {
  return new URL(String(fetch.mock.calls[call][1]), 'https://agent.example');
}

function control(overrides: Partial<ProfilingControlSnapshot> = {}): ProfilingControlSnapshot {
  return {
    deployment: 'test',
    mode: 'admin',
    revision: 12,
    configured: true,
    server_time: '2026-09-21T10:00:00Z',
    status: 'off',
    run: null,
    run_active: false,
    last_stopped_run: null,
    stored_samples: 0,
    budget_scope: 'producer_instance',
    budget_note: 'Each producer enforces its own budget.',
    sync_note: 'the configuration reaches every process through a background sync',
    operations: {
      selectable: {
        sandbox_startup: ['attempt.startup'],
        terminal_open: ['terminal.open'],
        file_access: ['files.list', 'files.open'],
      },
      all: ['attempt.startup', 'terminal.open', 'files.list', 'files.open'],
    },
    bounds: {
      min_duration_seconds: 60,
      max_duration_seconds: 3600,
      default_max_operations: 100,
      hard_max_operations: 1000,
    },
    ...overrides,
  };
}

describe('admin measurements client', () => {
  it('reads the catalogue with its window and page bound, and no cache', async () => {
    const fetch = stub();
    const controller = new AbortController();
    await listProfilingRuns({ since: '2026-09-03T00:00:00.000Z', limit: 25 }, controller.signal);

    const url = urlOf(fetch);
    expect(url.pathname).toBe('/admin/agent/profiling-runs');
    expect(url.searchParams.get('since')).toBe('2026-09-03T00:00:00.000Z');
    expect(url.searchParams.get('limit')).toBe('25');
    expect(fetch.mock.calls[0][2]).toEqual({ signal: controller.signal, cache: 'no-store' });
  });

  it('asks the catalogue for nothing it was not given', async () => {
    const fetch = stub();
    await listProfilingRuns();
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/profiling-runs');
  });

  it('escapes an experiment id rather than pasting it into the path', async () => {
    const fetch = stub();
    await summarizeProfilingRun('run/one two');
    expect(String(fetch.mock.calls[0][1])).toBe(
      '/admin/agent/profiling-runs/run%2Fone%20two/summary',
    );
  });

  it('repeats each grouping dimension instead of joining them', async () => {
    const fetch = stub();
    await summarizeProfilingRun('run-1', ['startup_path', 'coverage_scope']);
    const url = urlOf(fetch);
    // A joined value would name one dimension that does not exist, and the
    // server would refuse the whole read.
    expect(url.searchParams.getAll('group_by')).toEqual(['startup_path', 'coverage_scope']);
  });

  it('repeats every repeatable measurement filter', async () => {
    const fetch = stub();
    await listMeasurements({
      run_id: 'run-1',
      operation: ['files.list', 'files.open'],
      request_kind: ['terminal.create'],
      coverage_scope: ['server_request', 'host_startup'],
      outcome: 'completed',
      collection_complete: true,
      job_id: 'ajob_1',
      thread_id: 'athr_1',
      attempt_id: 42,
      interaction_id: 'aint_1',
      after: 'msm_9',
      limit: 50,
    });

    const url = urlOf(fetch);
    expect(url.pathname).toBe('/admin/agent/measurements');
    expect(url.searchParams.getAll('operation')).toEqual(['files.list', 'files.open']);
    expect(url.searchParams.getAll('request_kind')).toEqual(['terminal.create']);
    expect(url.searchParams.getAll('coverage_scope')).toEqual(['server_request', 'host_startup']);
    expect(url.searchParams.get('outcome')).toBe('completed');
    expect(url.searchParams.get('collection_complete')).toBe('true');
    expect(url.searchParams.get('job_id')).toBe('ajob_1');
    expect(url.searchParams.get('thread_id')).toBe('athr_1');
    // An attempt and an interaction are distinct keys, not two spellings of
    // one: they select different records and the server treats them so.
    expect(url.searchParams.get('attempt_id')).toBe('42');
    expect(url.searchParams.get('interaction_id')).toBe('aint_1');
    expect(url.searchParams.get('after')).toBe('msm_9');
    expect(url.searchParams.get('limit')).toBe('50');
  });

  it('sends attempt_id=0 rather than treating a defined number as absent', async () => {
    const fetch = stub();
    await listMeasurements({ attempt_id: 0 });
    // Only `undefined` means "any attempt": a defined number is sent, so the
    // client cannot silently widen a selection it was handed.
    expect(urlOf(fetch).searchParams.get('attempt_id')).toBe('0');
  });

  it('omits an empty interaction id instead of filtering for the empty string', async () => {
    const fetch = stub();
    await listMeasurements({ interaction_id: '' });
    expect(urlOf(fetch).search).toBe('');
  });

  it('sends collection_complete=false rather than dropping it', async () => {
    const fetch = stub();
    await listMeasurements({ collection_complete: false });
    // Falsy is a selection here, not an absence: dropping it would answer with
    // complete and partial records together.
    expect(urlOf(fetch).searchParams.get('collection_complete')).toBe('false');
  });

  it('omits an empty or absent filter instead of sending an empty selector', async () => {
    const fetch = stub();
    await listMeasurements({ operation: [], request_kind: [''], outcome: '', job_id: undefined });
    const url = urlOf(fetch);
    expect(url.search).toBe('');
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/measurements');
  });

  it('reads one record by id without a cache', async () => {
    const fetch = stub();
    const controller = new AbortController();
    await getMeasurement('msm_1 2', controller.signal);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/measurements/msm_1%202');
    expect(fetch.mock.calls[0][2]).toEqual({ signal: controller.signal, cache: 'no-store' });
  });

  it('reads the local writer counters without a cache', async () => {
    const fetch = stub();
    const controller = new AbortController();
    await getMeasurementWriterStatus(controller.signal);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/measurement-writer');
    expect(fetch.mock.calls[0][2]).toEqual({ signal: controller.signal, cache: 'no-store' });
  });

  it('reads the runtime control snapshot without a cache', async () => {
    const fetch = stub();
    const controller = new AbortController();
    await getProfilingControl(controller.signal);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/profiling-control');
    // The status is a statement about now: a cached one describes a moment
    // nobody chose, which for a control surface is the whole question.
    expect(fetch.mock.calls[0][2]).toEqual({ signal: controller.signal, cache: 'no-store' });
  });

  it('posts a start command as JSON, echoing the revision it read, and no cache', async () => {
    const fetch = stub();
    const controller = new AbortController();
    const body = {
      request_id: 'req-1',
      expected_revision: 12,
      operations: ['attempt.startup', 'terminal.open'],
      target: { host: 'gpu-1' },
      duration_seconds: 600,
    };
    await startProfilingRun(body, controller.signal);

    // The collection route is the same resource the reads above use; creating
    // one is a POST to it, not a new path.
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/profiling-runs');
    expect(fetch.mock.calls[0][2]).toEqual({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
      cache: 'no-store',
    });
  });

  it('sends an all-hosts target as null rather than omitting the key', async () => {
    const fetch = stub();
    await startProfilingRun({
      request_id: 'req-1',
      expected_revision: 0,
      operations: ['files.open'],
      target: { host: null },
      duration_seconds: 60,
    });
    expect(JSON.parse(String(fetch.mock.calls[0][2]?.body))).toEqual({
      request_id: 'req-1',
      expected_revision: 0,
      operations: ['files.open'],
      target: { host: null },
      duration_seconds: 60,
    });
  });

  it('stops one run by escaped id, with an idempotent request id, and no cache', async () => {
    const fetch = stub();
    const controller = new AbortController();
    await stopProfilingRun('run/one two', { request_id: 'req-2' }, controller.signal);

    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/profiling-runs/run%2Fone%20two/stop');
    expect(fetch.mock.calls[0][2]).toEqual({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: 'req-2' }),
      signal: controller.signal,
      cache: 'no-store',
    });
  });

  it('maps each checked group onto the root operation names the server offers', () => {
    const snapshot = control();
    expect(measurementOperationsFor(['sandbox_startup'], snapshot)).toEqual(['attempt.startup']);
    expect(measurementOperationsFor(['terminal_open', 'file_access'], snapshot)).toEqual([
      'terminal.open',
      'files.list',
      'files.open',
    ]);
    expect(measurementOperationsFor([], snapshot)).toEqual([]);
  });

  it('asks for nothing when the deployment offers nothing for a group', () => {
    const snapshot = control({
      operations: {
        selectable: { sandbox_startup: [], terminal_open: ['terminal.open'], file_access: [] },
        all: ['terminal.open'],
      },
    });
    // An empty list is the honest answer: the server would refuse a request
    // naming an operation this deployment does not select.
    expect(measurementOperationsFor(['sandbox_startup', 'file_access'], snapshot)).toEqual([]);
    expect(measurementOperationsFor(['sandbox_startup', 'terminal_open'], snapshot)).toEqual([
      'terminal.open',
    ]);
  });

  it('names a shared operation once, in the order the groups were given', () => {
    const snapshot = control({
      operations: {
        selectable: {
          sandbox_startup: ['attempt.startup'],
          terminal_open: ['files.open'],
          file_access: ['files.open', 'files.list'],
        },
        all: ['attempt.startup', 'files.open', 'files.list'],
      },
    });
    expect(measurementOperationsFor(['file_access', 'terminal_open'], snapshot)).toEqual([
      'files.open',
      'files.list',
    ]);
  });
});
