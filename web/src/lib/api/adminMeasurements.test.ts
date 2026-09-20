import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  getMeasurement,
  getMeasurementWriterStatus,
  listMeasurements,
  listProfilingRuns,
  summarizeProfilingRun,
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
});
