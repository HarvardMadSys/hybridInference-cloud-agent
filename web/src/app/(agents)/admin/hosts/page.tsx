'use client';

// The runner host pool — the whole of this service's operations surface.
//
// Runners pull work, so nothing here moves a sandbox to a machine. The pin
// chooses where chats without a sandbox may be materialized; an existing
// retained chat remains claimable only by its Docker-owning host. "Pinned",
// not "assigned"; "last polled", not "online".
//
// Enrolment is deliberately absent. Minting a host credential is a real
// operation and its endpoint stays, but it exists to stop a machine claiming
// to be a machine it is not — and every runner in this pool is ours, so the
// control was answering a question nobody here asks. Issue a token from the
// API on the rare day a new host needs one.

import { useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type { AgentRunnerHost, AgentRunnerHostPool } from '@/lib/api/admin';
import {
  drainAgentRunnerHost,
  getAgentRunnerHosts,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  setAgentRuntimeVersion,
  undrainAgentRunnerHost,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

import { formatAge, formatSlots, runtimeMenu, serverInventory } from './format';

// A runner polls only between jobs, so silence can mean "busy for an hour" as
// easily as "gone". Carried over from the gateway's section along with the
// wording it justifies.
const RECENT_POLL_SECONDS = 300;

// Well under the staleness threshold, so a host that stops polling is reported
// as stale within a poll or two of actually becoming so.
const REFRESH_MS = 30_000;

const STATUS_STYLES: Record<string, string> = {
  online:
    'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 ring-emerald-600/20 dark:ring-emerald-500/25',
  draining:
    'bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400 ring-amber-600/20 dark:ring-amber-500/25',
  enrolling:
    'bg-blue-50 dark:bg-blue-500/10 text-blue-700 dark:text-blue-400 ring-blue-600/20 dark:ring-blue-500/25',
  disabled:
    'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400 ring-gray-500/20 dark:ring-white/15',
};

function StatusBadge({ status }: { status: string }) {
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.disabled;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${style}`}
    >
      {status}
    </span>
  );
}

export default function AdminHostsPage() {
  const { state } = useAuth();
  const [hosts, setHosts] = useState<AgentRunnerHost[]>([]);
  const [executionProfile, setExecutionProfile] = useState('untrusted-kata-v1');
  const [activeHost, setActiveHost] = useState<string | null>(null);
  const [runtimeVersions, setRuntimeVersions] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const mounted = useRef(true);
  const isAdmin = hasAgentPermission(state.user?.agent_access, 'agent.admin');

  const load = useCallback(async (background = false) => {
    if (!background) setLoading(true);
    setError(null);
    try {
      const next = await getAgentRunnerHosts();
      if (!mounted.current) return;
      setHosts(next.hosts);
      setExecutionProfile(next.execution_profile ?? 'untrusted-kata-v1');
      setActiveHost(next.active_host);
      setRuntimeVersions(next.runtime_versions ?? {});
    } catch (e) {
      if (!mounted.current) return;
      // A failed background poll leaves the last good pool on screen rather
      // than replacing it with an error: the numbers going stale is a smaller
      // lie than the page claiming there is nothing there.
      if (!background) setError(getErrorMessage(e));
    } finally {
      if (mounted.current && !background) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    if (!isAdmin) {
      setLoading(false);
      return () => {
        mounted.current = false;
      };
    }
    void load();
    // Everything here is time-relative — poll ages, and the warning that fires
    // when the pinned host goes quiet. Fetched once, the ages freeze at whatever
    // they were when the tab was opened and the warning can never appear for a
    // machine that dies afterwards, which is exactly when an operator is
    // looking at this page.
    const timer = setInterval(() => void load(true), REFRESH_MS);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, [load, isAdmin]);

  // Every action returns the whole pool, so they all end the same way.
  const run = useCallback(async (action: () => Promise<AgentRunnerHostPool>, ok: string) => {
    setBusy(true);
    setNotice(null);
    try {
      const next = await action();
      if (!mounted.current) return;
      setHosts(next.hosts);
      setExecutionProfile(next.execution_profile ?? 'untrusted-kata-v1');
      setActiveHost(next.active_host);
      setRuntimeVersions(next.runtime_versions ?? {});
      setNotice(ok);
    } catch (e) {
      if (mounted.current) setNotice(getErrorMessage(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);

  const handlePin = (host: string | null) =>
    run(
      () => setActiveAgentRunnerHost(host),
      host
        ? `New chats now go to ${serverInventory(hosts, executionProfile).find((server) => server.host?.host === host)?.name ?? host}. Existing retained chats stay on their owning hosts.`
        : 'New chats are assigned automatically. Existing retained chats stay on their owning hosts.',
    );

  // Unioned across the pool, because the selection is deployment-wide while
  // the menus are per-image. Derived on render rather than held in state: it is
  // a view of `hosts`, and a second copy could disagree with the table beside
  // it after a poll.
  const servers = serverInventory(hosts, executionProfile);
  const availableServers = servers.filter((server) => server.available);
  const visibleServers = servers.filter(
    (server) => server.host && server.host.status !== 'disabled',
  );
  const inactiveServers = servers.filter(
    (server) => !server.host || server.host.status === 'disabled',
  );
  const selectedServer = servers.find((server) => server.selected);
  const selectionUnavailable = Boolean(
    activeHost &&
    !hosts.some(
      (host) =>
        host.host === activeHost &&
        host.enrolled &&
        host.status === 'online' &&
        (executionProfile === 'trusted-v1' || host.sandbox_runtime === 'io.containerd.kata.v2'),
    ),
  );
  const menu = runtimeMenu(visibleServers.flatMap((server) => (server.host ? [server.host] : [])));

  const handleVersion = (runtime: string, version: string) =>
    run(
      () => setAgentRuntimeVersion(runtime, version || null),
      version
        ? `New ${runtime} threads will run ${version}. Threads already running keep theirs.`
        : `${runtime} is back on each host's image default.`,
    );

  const handleRemove = (host: string, slotsUsed: number) => {
    // The unforced call 409s on a busy host, and the operator would then have
    // to decide anyway — with the consequence stated, before the request.
    if (slotsUsed > 0) {
      const ok = window.confirm(
        `${host} is still running ${slotsUsed} attempt(s). Removing it does not stop them — ` +
          'nothing here can reach them. They are left to their leases, which expire and requeue ' +
          'the work exactly once. Remove anyway?',
      );
      if (!ok) return;
    }
    void run(() => removeAgentRunnerHost(host, slotsUsed > 0), `Removed ${host} from the pool.`);
  };

  if (!isAdmin) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
          Admin access required
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          The runner host pool is visible to admins only.
        </p>
      </div>
    );
  }

  // Silence blocks new-chat placement on the selected host. Existing retained
  // chats have the same host-local dependency on each of their own owners.
  const staleActive = hosts.find((h) => h.active && h.seconds_since_seen > RECENT_POLL_SECONDS);

  return (
    <div className="flex-1 overflow-y-auto">
      {/* The page shell the rest of the app uses (see IntegrationsView): one
          wide column, a real heading, and sections that are cards rather than
          controls floating between paragraphs. This page had grown its own
          denser dialect, which read as unfinished beside every other screen. */}
      <section className="mx-auto w-full max-w-5xl px-6 py-12 lg:px-12 lg:py-16">
        {/* This is the admin area, not a page about one subject — which is
            what "Runner hosts" made it, with the harness selector reading as a
            sub-heading of the machine list. A CLI version is not a property of
            a machine; both are settings an admin comes here to change, and
            naming the container rather than one of its contents leaves room
            for the next one without another rename.

            No subtitle: the two section headings already say what is here, and
            a sentence restating them is the prose this page kept accumulating. */}
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>

        {error && (
          <div className="mt-6 flex items-center justify-between gap-4 rounded-xl border border-red-200 dark:border-red-500/25 bg-red-50 dark:bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-400">
            <span>{error}</span>
            <button
              type="button"
              onClick={() => void load()}
              className="shrink-0 font-medium hover:underline"
            >
              Retry
            </button>
          </div>
        )}

        {notice && (
          <div
            className="mt-6 rounded-xl border border-gray-200 dark:border-white/10 bg-gray-50 dark:bg-white/5 px-4 py-3 text-sm text-gray-700 dark:text-gray-300"
            role="status"
          >
            {notice}
          </div>
        )}

        {staleActive && (
          <div className="mt-6 rounded-xl border border-amber-200 dark:border-amber-500/25 bg-amber-50 dark:bg-amber-500/10 px-4 py-3 text-sm text-amber-800 dark:text-amber-300">
            {staleActive.host} last polled {formatAge(staleActive.seconds_since_seen)}. New chats
            may be unavailable. Existing retained chats stay on their owning server.
          </div>
        )}

        {/* ── Harness version ──────────────────────────────────────────
            One row per runtime, because the previous inline strip put five
            selects and five bare runtime names on a single line: five copies
            of "Image default" with nothing to say which was which, and no
            room to explain that the options come from the image.

            Two layers the wording has to keep apart: the *menu* is built into
            each sandbox image and changes only by rebuilding; this picks which
            item on it new work runs. A runtime with one option is an image
            holding one version — add more through that host's
            `*_EXTRA_VERSIONS`. */}
        {menu.length > 0 && (
          <div className="mt-10">
            {/* No subtitle, unlike the section below, because both of the
                things one would say are already said where they land: that a
                running conversation keeps its version is in the confirmation
                after the change, and where the options come from — and how to
                add one — is deployment documentation, in `.env.example` and
                ENVIRONMENT.md, read by the person editing the `.env` rather
                than by anyone standing on this page. */}
            <h2 className="text-sm font-medium text-gray-600 dark:text-gray-400">
              Harness version
            </h2>
            <div className="mt-4 rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-6 shadow-sm">
              {/* A name and its control. Nothing here pre-announces what opening
                  the control would show: the versions on offer are in the
                  select, and the version each host is actually holding is in
                  the table below — said twice already, and the second telling
                  was the abstract one. */}
              {menu.map(([runtime, versions], index) => (
                <div
                  key={runtime}
                  className={`flex flex-col justify-center gap-3 py-2.5 sm:flex-row sm:items-center ${
                    index > 0 ? 'border-t border-gray-100 dark:border-white/5' : ''
                  }`}
                >
                  <div className="min-w-0 flex-1">
                    <label
                      htmlFor={`harness-version-${runtime}`}
                      className="text-sm text-gray-700 dark:text-gray-300"
                    >
                      {runtime}
                    </label>
                  </div>
                  <div className="flex shrink-0 justify-end">
                    <select
                      id={`harness-version-${runtime}`}
                      disabled={busy}
                      value={runtimeVersions[runtime] ?? ''}
                      onChange={(event) => void handleVersion(runtime, event.target.value)}
                      className="rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 shadow-sm hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
                    >
                      <option value="">Image default</option>
                      {versions.map((version) => (
                        <option key={version} value={version}>
                          {version}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Physical inventory groups independently enrolled execution environments.
            Actions still target the original deployment identity. */}
        <div className="mt-10">
          <h2 className="text-sm font-medium text-gray-600 dark:text-gray-400">Servers</h2>

          {availableServers.length > 1 && (
            <div className="mt-3 flex items-center gap-3">
              <label
                htmlFor="server-placement"
                className="text-sm text-gray-600 dark:text-gray-400"
              >
                New chats
              </label>
              <select
                id="server-placement"
                disabled={busy}
                value={
                  activeHost
                    ? selectionUnavailable
                      ? '__unavailable__'
                      : (selectedServer?.name ?? '__unavailable__')
                    : ''
                }
                onChange={(event) => {
                  const chosen = availableServers.find(
                    (server) => server.name === event.target.value,
                  );
                  void handlePin(chosen?.host?.host ?? null);
                }}
                className="rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-1.5 text-sm"
              >
                <option value="">Automatic</option>
                {selectionUnavailable && (
                  <option value="__unavailable__" disabled>
                    Selected server unavailable
                  </option>
                )}
                {availableServers.map((server) => (
                  <option key={server.name} value={server.name}>
                    {server.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          {selectionUnavailable && (
            <div role="alert" className="mt-3 text-sm text-amber-700 dark:text-amber-300">
              The selected server cannot accept new chats.
              <button
                type="button"
                disabled={busy}
                onClick={() => void handlePin(null)}
                className="ml-2 underline"
              >
                Clear unavailable selection
              </button>
            </div>
          )}

          {loading ? (
            <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">Loading…</p>
          ) : (
            <>
              {visibleServers.length === 0 ? (
                <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">
                  No servers available.
                </p>
              ) : (
                <div className="mt-4 overflow-x-auto rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 shadow-sm">
                  <table className="min-w-full text-left text-sm">
                    <thead className="border-b border-gray-100 dark:border-white/5 text-xs text-gray-500 dark:text-gray-400">
                      <tr>
                        <th className="px-6 py-3 font-medium">Server</th>
                        <th className="px-3 py-3 font-medium">Status</th>
                        <th className="px-3 py-3 font-medium">Slots</th>
                        <th className="px-3 py-3 font-medium whitespace-nowrap">Last polled</th>
                        <th className="px-6 py-3 text-right font-medium">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                      {visibleServers.map(
                        ({ name, host }) =>
                          host && (
                            <tr key={name} className="align-top">
                              <th
                                scope="row"
                                className="px-6 py-4 font-medium text-gray-900 dark:text-gray-50"
                              >
                                {name}
                              </th>
                              <td className="px-3 py-4">
                                <StatusBadge status={host.status} />
                              </td>
                              <td className="px-3 py-4 whitespace-nowrap">{formatSlots(host)}</td>
                              <td className="px-3 py-4 whitespace-nowrap">
                                {formatAge(host.seconds_since_seen)}
                              </td>
                              <td className="px-6 py-4">
                                <div className="flex items-center justify-end gap-2">
                                  <button
                                    type="button"
                                    disabled={busy}
                                    onClick={() =>
                                      void run(
                                        () =>
                                          host.status === 'draining'
                                            ? undrainAgentRunnerHost(host.host)
                                            : drainAgentRunnerHost(host.host),
                                        host.status === 'draining'
                                          ? `${name} is taking work again.`
                                          : `${name} stops taking new work; running attempts finish.`,
                                      )
                                    }
                                    className="rounded-lg border border-gray-200 dark:border-white/10 px-2.5 py-1 text-xs disabled:opacity-40"
                                  >
                                    {host.status === 'draining' ? 'Undrain' : 'Drain'}
                                  </button>
                                  <button
                                    type="button"
                                    disabled={busy}
                                    onClick={() => handleRemove(host.host, host.slots_used)}
                                    className="rounded-lg px-2.5 py-1 text-xs text-red-600 dark:text-red-400 disabled:opacity-40"
                                  >
                                    Remove
                                  </button>
                                </div>
                              </td>
                            </tr>
                          ),
                      )}
                    </tbody>
                  </table>
                </div>
              )}
              {inactiveServers.length > 0 && (
                <details className="mt-4 text-sm text-gray-500 dark:text-gray-400">
                  <summary className="cursor-pointer">
                    Unavailable servers ({inactiveServers.length})
                  </summary>
                  <ul className="mt-2 space-y-1">
                    {inactiveServers.map((server) => (
                      <li key={server.name}>
                        {server.name} — {server.host?.status ?? 'unavailable'}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  );
}
