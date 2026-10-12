import type { AdminSandbox } from '@/lib/api/adminSandboxes';

export const STALE_RESOURCE_SECONDS = 90;

/** Each row keeps its own monotonic page receipt time through pagination. */
export interface SandboxObservation {
  sandbox: AdminSandbox;
  observedAtMs: number;
  serverObservedAt?: string;
}

export function finiteMetric(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function formatBytes(value: number | null | undefined): string {
  const amount = finiteMetric(value);
  if (amount === null) return 'Unavailable';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const index = amount === 0 ? 0 : Math.min(4, Math.floor(Math.log(amount) / Math.log(1024)));
  const safeIndex = Math.max(0, index);
  const converted = amount / 1024 ** safeIndex;
  return `${converted.toLocaleString('en-US', { maximumFractionDigits: 1 })} ${units[safeIndex]}`;
}

export function formatCpu(value: number | null | undefined): string {
  const amount = finiteMetric(value);
  return amount === null ? 'Unavailable' : `${amount.toFixed(2)} cores`;
}

export function formatDiskLimit(value: number | undefined): string {
  if (value === undefined || value === 0) return 'No disk quota';
  const amount = finiteMetric(value);
  if (amount === null) return 'Unavailable disk quota';
  return amount % 1024 === 0 ? `${amount / 1024} GiB disk` : `${amount} MiB disk`;
}

export function formatRate(value: number | null | undefined): string {
  return finiteMetric(value) === null ? 'Unavailable' : `${formatBytes(value)}/s`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return 'Unavailable';
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m ${value % 60}s`;
  if (value < 86400) return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
  return `${Math.floor(value / 86400)}d ${Math.floor((value % 86400) / 3600)}h`;
}

export function sampleFreshness(sandbox: AdminSandbox, observedAtMs: number, nowMs: number) {
  const usage = sandbox.resource_usage;
  const initialAge = finiteMetric(usage?.age_seconds);
  const ageSeconds =
    initialAge === null ? null : initialAge + Math.max(0, nowMs - observedAtMs) / 1000;
  return {
    ageSeconds,
    stale: !usage || ageSeconds === null || usage.stale || ageSeconds > STALE_RESOURCE_SECONDS,
  };
}

export function summarizeSandboxes(observations: SandboxObservation[], nowMs: number) {
  const summary = {
    sandboxCount: observations.length,
    activeTurns: 0,
    sessions: 0,
    hosts: new Set<string>(),
    cpuCores: 0,
    memoryBytes: 0,
    cpuCoverage: 0,
    memoryCoverage: 0,
    sampleCoverage: 0,
  };
  for (const { sandbox, observedAtMs } of observations) {
    summary.activeTurns += finiteMetric(sandbox.active_attempt_count) ?? 0;
    summary.sessions += finiteMetric(sandbox.member_count) ?? 0;
    summary.hosts.add(sandbox.host);
    if (sampleFreshness(sandbox, observedAtMs, nowMs).stale) continue;
    summary.sampleCoverage += 1;
    const cpu = finiteMetric(sandbox.resource_usage?.cpu_cores);
    if (cpu !== null) {
      summary.cpuCores += cpu;
      summary.cpuCoverage += 1;
    }
    const memory = finiteMetric(sandbox.resource_usage?.memory_bytes);
    if (memory !== null) {
      summary.memoryBytes += memory;
      summary.memoryCoverage += 1;
    }
  }
  return { ...summary, hosts: summary.hosts.size };
}
