// Cell formatting for the host table.
//
// **A separate module because `page.tsx` may not export anything else.** Next
// type-checks a page's exports against a fixed shape, so a named helper
// exported beside the default component fails the build — and it fails at
// `next build`, not in lint, tests or `tsc --noEmit`. Tested directly here
// rather than through the rendered table, which is what the extra exports were
// for.

import type { AgentRunnerHost } from '@/lib/api/admin';

export function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** Slots as "2 / 4", or "2" where the host declares no ceiling. */
export function formatSlots(host: AgentRunnerHost): string {
  return host.slots_total === null
    ? `${host.slots_used}`
    : `${host.slots_used} / ${host.slots_total}`;
}

/**
 * Every (runtime, versions) pair the pool as a whole offers.
 *
 * Unioned across hosts because the selection is deployment-wide while the menus
 * are per-image: with two hosts on different images, a version only one of them
 * holds is still selectable — the claim falls back on the host that lacks it
 * rather than refusing, which is the documented behaviour and better than
 * hiding the option. Sorted so the list does not reshuffle between polls.
 */
export function runtimeMenu(hosts: AgentRunnerHost[]): [string, string[]][] {
  const menu = new Map<string, string[]>();
  for (const host of hosts) {
    for (const [runtime, versions] of Object.entries(host.runtime_versions_available ?? {})) {
      const seen = menu.get(runtime) ?? [];
      // Order is the image's — default first — and preserved for the host that
      // introduced each version, so the first option is a real default.
      menu.set(runtime, [...seen, ...versions.filter((v) => !seen.includes(v))]);
    }
  }
  return [...menu.entries()].sort(([a], [b]) => a.localeCompare(b));
}

/** Physical inventory only. Deployment IDs remain the targets for every action. */
export function groupHosts(hosts: AgentRunnerHost[]): [string, AgentRunnerHost[]][] {
  const groups = new Map<string, AgentRunnerHost[]>();
  for (const host of hosts) {
    const reported = host.labels?.physical_host;
    const server = typeof reported === 'string' && reported.trim() ? reported.trim() : host.host;
    const deployments = groups.get(server) ?? [];
    deployments.push(host);
    groups.set(server, deployments);
  }
  return [...groups.entries()];
}

export function executionEnvironment(host: AgentRunnerHost): string {
  if (host.sandbox_runtime === 'io.containerd.kata.v2') return 'Kata';
  if (host.sandbox_runtime === '' || host.sandbox_runtime === 'runc') return 'Standard (runc)';
  return host.sandbox_runtime ?? 'Runtime unreported';
}
