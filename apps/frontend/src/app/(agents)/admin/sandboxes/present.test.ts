import { describe, expect, it } from 'vitest';

import type { AdminSandbox, AdminSandboxResourceUsage } from '@/lib/api/adminSandboxes';

import {
  finiteMetric,
  formatBytes,
  formatCpu,
  sampleFreshness,
  summarizeSandboxes,
} from './present';

function sandbox(overrides: Partial<AdminSandbox> = {}): AdminSandbox {
  return {
    id: 'sandbox-one',
    state: 'running',
    origin_thread_id: 'thread-one',
    title: 'Fix login',
    repo: 'org/repo',
    user_id: 'owner-one',
    host: 'host-one',
    container_id: 'container-one-id',
    container_name: 'container-one',
    runtime_id: 'codex',
    runtime_version: '1.2.3',
    execution_profile: 'trusted-v1',
    memory_limit: '4g',
    cpu_limit: '2',
    member_count: 3,
    active_attempt_count: 1,
    created_at: '2026-10-10T09:00:00Z',
    last_activity_at: '2026-10-10T09:59:00Z',
    idle_deadline: '2026-10-10T10:15:00Z',
    updated_at: '2026-10-10T09:59:50Z',
    host_last_heartbeat_at: '2026-10-10T10:00:00Z',
    ...overrides,
  };
}

function usage(overrides: Partial<AdminSandboxResourceUsage> = {}): AdminSandboxResourceUsage {
  return {
    source: 'docker-stats',
    scope: 'container',
    sample_id: 'sample-one',
    sampled_at: '2026-10-10T09:59:55Z',
    received_at: '2026-10-10T10:00:00Z',
    age_seconds: 5,
    stale: false,
    cpu_cores: 0.25,
    cpu_window_seconds: 30,
    memory_bytes: 1024 ** 2,
    memory_limit_bytes: 4 * 1024 ** 3,
    pids: 2,
    io_read_bytes: 0,
    io_write_bytes: 1024,
    net_rx_bytes: 2048,
    net_tx_bytes: 4096,
    io_read_bytes_per_second: 0,
    io_write_bytes_per_second: 1,
    net_rx_bytes_per_second: 2,
    net_tx_bytes_per_second: 4,
    ...overrides,
  };
}

describe('sandbox resource formatting', () => {
  it('keeps a measured zero distinct from missing or unusable metrics', () => {
    expect(finiteMetric(0)).toBe(0);
    expect(finiteMetric(1.25)).toBe(1.25);
    for (const value of [undefined, null, -1, NaN, Infinity, -Infinity]) {
      expect(finiteMetric(value)).toBeNull();
    }
    expect(formatBytes(0)).toBe('0 B');
    expect(formatCpu(0)).toBe('0.00 cores');
    expect(formatBytes(null)).toBe('Unavailable');
    expect(formatCpu(undefined)).toBe('Unavailable');
  });

  it('formats CPU in cores and byte counters with binary units', () => {
    expect(formatCpu(0.125)).toBe('0.13 cores');
    expect(formatCpu(2.5)).toBe('2.50 cores');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1536)).toBe('1.5 KiB');
    expect(formatBytes(1.5 * 1024 ** 2)).toBe('1.5 MiB');
    expect(formatBytes(1.5 * 1024 ** 3)).toBe('1.5 GiB');
    expect(formatBytes(-1)).toBe('Unavailable');
    expect(formatCpu(Infinity)).toBe('Unavailable');
  });
});

describe('sandbox resource freshness and fleet totals', () => {
  it('does not count an older host without a resource sample as measured zero', () => {
    expect(sampleFreshness(sandbox(), 10_000, 20_000)).toEqual({
      ageSeconds: null,
      stale: true,
    });
    expect(
      summarizeSandboxes([{ sandbox: sandbox(), observedAtMs: 10_000 }], 20_000),
    ).toMatchObject({
      sandboxCount: 1,
      activeTurns: 1,
      sessions: 3,
      hosts: 1,
      cpuCoverage: 0,
      memoryCoverage: 0,
      sampleCoverage: 0,
    });
  });

  it('ages a sample from its own page receipt through slow pagination', () => {
    const row = sandbox({ resource_usage: usage({ age_seconds: 75 }) });
    expect(sampleFreshness(row, 10_000, 25_000)).toEqual({ ageSeconds: 90, stale: false });
    expect(sampleFreshness(row, 10_000, 25_001)).toEqual({ ageSeconds: 90.001, stale: true });
    expect(sampleFreshness(row, 10_000, 5_000)).toEqual({ ageSeconds: 75, stale: false });
  });

  it('honors server staleness and excludes samples without a valid age', () => {
    expect(
      sampleFreshness(sandbox({ resource_usage: usage({ age_seconds: 0, stale: true }) }), 0, 0),
    ).toEqual({ ageSeconds: 0, stale: true });
    expect(sampleFreshness(sandbox({ resource_usage: usage({ age_seconds: NaN }) }), 0, 0)).toEqual(
      { ageSeconds: null, stale: true },
    );
  });

  it('sums only fresh measurements and reports independent coverage for CPU and memory', () => {
    const rows = [
      sandbox({ resource_usage: usage({ cpu_cores: 0, memory_bytes: 0 }) }),
      sandbox({
        id: 'sandbox-two',
        active_attempt_count: 2,
        member_count: 1,
        resource_usage: usage({ cpu_cores: 1.5, memory_bytes: null }),
      }),
      sandbox({
        id: 'sandbox-three',
        host: 'host-two',
        active_attempt_count: 0,
        member_count: 2,
        resource_usage: usage({ cpu_cores: null, memory_bytes: 1024 }),
      }),
      sandbox({
        id: 'sandbox-four',
        host: 'host-two',
        active_attempt_count: 0,
        member_count: 0,
        resource_usage: usage({ cpu_cores: 100, memory_bytes: 1_000_000, age_seconds: 91 }),
      }),
      sandbox({
        id: 'sandbox-five',
        host: 'host-three',
        active_attempt_count: 0,
        member_count: 1,
      }),
    ];
    expect(
      summarizeSandboxes(
        rows.map((row) => ({ sandbox: row, observedAtMs: 10_000 })),
        10_000,
      ),
    ).toEqual({
      sandboxCount: 5,
      activeTurns: 3,
      sessions: 7,
      hosts: 3,
      cpuCores: 1.5,
      memoryBytes: 1024,
      cpuCoverage: 2,
      memoryCoverage: 2,
      sampleCoverage: 3,
    });
  });

  it('ignores negative and non-finite samples instead of contaminating fleet totals', () => {
    const rows = [
      sandbox({ resource_usage: usage({ cpu_cores: -1, memory_bytes: Infinity }) }),
      sandbox({ id: 'sandbox-two', resource_usage: usage({ cpu_cores: NaN, memory_bytes: -1 }) }),
    ];
    expect(
      summarizeSandboxes(
        rows.map((row) => ({ sandbox: row, observedAtMs: 0 })),
        0,
      ),
    ).toMatchObject({
      cpuCores: 0,
      memoryBytes: 0,
      cpuCoverage: 0,
      memoryCoverage: 0,
      sampleCoverage: 2,
    });
  });

  it('preserves each page receipt time when measuring the fleet', () => {
    expect(
      summarizeSandboxes(
        [
          {
            sandbox: sandbox({ resource_usage: usage({ age_seconds: 0, cpu_cores: 8 }) }),
            observedAtMs: 0,
          },
          {
            sandbox: sandbox({
              id: 'sandbox-two',
              resource_usage: usage({ age_seconds: 0, cpu_cores: 0 }),
            }),
            observedAtMs: 100_000,
          },
        ],
        100_000,
      ),
    ).toMatchObject({ cpuCores: 0, cpuCoverage: 1, memoryCoverage: 1, sampleCoverage: 1 });
  });
});
