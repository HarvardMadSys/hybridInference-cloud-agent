// Cell formatting for the host table.
//
// **A separate module because `page.tsx` may not export anything else.** Next
// type-checks a page's exports against a fixed shape, so a named helper
// exported beside the default component fails the build — and it fails at
// `next build`, not in lint, tests or `tsc --noEmit`. Tested directly here
// rather than through the rendered table, which is what the extra exports were
// for.

import type { AgentRunnerHost } from '@/lib/api/admin';

/**
 * A host's status as an operator reads it.
 *
 * `draining` is never something an operator chooses on this page: it is the
 * step a Remove or a deployment takes while running turns finish. So it is
 * named by what it is on the way to — a removal, an update — and only a drain
 * started outside the page, through the API, reads as plainly paused.
 */
export function hostStatusLabel(host: {
  status: string;
  drain_fence_active?: boolean;
  removal_pending?: boolean;
}): string {
  if (host.status !== 'draining') return host.status;
  if (host.removal_pending) return 'removing';
  if (host.drain_fence_active) return 'updating';
  return 'paused';
}

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

/** Statuses in which an enrollment is still reporting in. */
const REPORTING_STATUSES = new Set(['online', 'draining']);

/**
 * Whether this enrollment is still reporting in.
 *
 * The one definition of it on this page. `draining` counts: it is an operator's
 * instruction to stop taking work, not silence — the machine is still there and
 * still sending its load.
 */
export function isReporting(host: AgentRunnerHost): boolean {
  return REPORTING_STATUSES.has(host.status);
}

/**
 * The enrollments a capacity curve may be drawn from.
 *
 * A physical name groups identities for display, and the rows behind it are
 * not all machines someone can look at. Two rules, in order:
 *
 *   * **A removed enrollment is never offered.** Its row survives on purpose —
 *     attempts name it and its history stays in the API — but its credential
 *     is revoked. Offering it is how a retired environment came to look like a
 *     second one.
 *   * **While any enrollment is reporting, only the reporting ones are
 *     offered.** A row that has stopped heartbeating under a name this machine
 *     no longer answers to is the machine's previous identity, not a second
 *     environment: the same box, renamed, whose old row the reaper marked
 *     offline and nothing cleans up. Its curve is the same machine's, before
 *     the rename, so nothing is lost by leaving it out of a picker.
 *
 * When *nothing* is reporting the fallback is deliberate: every non-removed
 * enrollment stays available, so a machine that is rebooting can still show
 * its last curve with the stale marker that says how old it is.
 */
export function curveSourceHosts(members: AgentRunnerHost[]): AgentRunnerHost[] {
  const live = members.filter((member) => member.status !== 'disabled');
  const reporting = live.filter((member) => isReporting(member));
  return reporting.length > 0 ? reporting : live;
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
