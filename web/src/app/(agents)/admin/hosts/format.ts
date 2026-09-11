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

/** A server exposes one executor for new chats; legacy owners stay in the API. */
export function serverInventory(hosts: AgentRunnerHost[], profile: string) {
  return groupHosts(hosts).map(([name, members]) => {
    const compatible = members.filter(
      (host) => profile === 'trusted-v1' || host.sandbox_runtime === 'io.containerd.kata.v2',
    );
    // Stable across heartbeat ordering. Preserve a current selection on this
    // server, otherwise use an online executor before an unavailable one.
    const ranked = [...compatible].sort(
      (a, b) =>
        Number(b.status === 'online') - Number(a.status === 'online') ||
        Number(b.active) - Number(a.active) ||
        a.host.localeCompare(b.host),
    );
    const host = ranked[0];
    return {
      name,
      host,
      // Every enrollment behind this physical name, for the capacity columns:
      // one member's CPU is the machine's, but retained sandboxes are counted
      // across all of them, and a legacy environment's containers are real.
      members,
      available: Boolean(host?.enrolled && host.status === 'online'),
      selected: members.some((member) => member.active),
    };
  });
}

/**
 * The enrollments a capacity curve may be drawn from.
 *
 * A physical name groups identities for display, and one of them can be an
 * environment that has been removed: its row survives on purpose — attempts
 * name it and its history stays in the API — but its credential is revoked and
 * it is no longer a machine anyone can read a curve from. Offering it is how
 * the panel's source control came to look like the server's own name repeated
 * with a suffix, and how a retired environment came to be mistaken for a
 * second one.
 *
 * Offline is not removed, and is deliberately kept. A host that missed its
 * heartbeats may be rebooting, its last curve is still this machine's, and the
 * stale marker is what says how old the reading is.
 */
export function curveSourceHosts(members: AgentRunnerHost[]): AgentRunnerHost[] {
  return members.filter((member) => member.status !== 'disabled');
}

/**
 * Which enrollment a curve opens on.
 *
 * The row's CPU and memory come from `preferredHost`, so the curve starts
 * there — unless that reading came from an environment this list does not
 * offer, in which case the representative stands in. Returns null when the
 * server has no environment a curve could be drawn from at all.
 */
export function curveSource(
  members: AgentRunnerHost[],
  preferredHost: string | null,
  representativeHost: string | null | undefined,
): AgentRunnerHost | null {
  const offered = curveSourceHosts(members);
  return (
    offered.find((member) => member.host === preferredHost) ??
    offered.find((member) => member.host === representativeHost) ??
    offered[0] ??
    null
  );
}
