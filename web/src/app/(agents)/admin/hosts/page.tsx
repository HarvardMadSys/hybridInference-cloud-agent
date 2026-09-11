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
//
// ── Capacity ──────────────────────────────────────────────────────────
//
// Each host now also reports what its Docker host is actually doing: CPU,
// memory, and how many sandboxes are retained. Three rules govern how it is
// shown, and each is the difference between a number and a claim:
//
//   * a missing measurement is never rendered as zero, and a stale one is
//     never rendered as current;
//   * an age is advanced by local monotonic time — the server's figure plus
//     `performance.now()` deltas — so a failed poll leaves the last good
//     numbers ticking upwards rather than frozen, and a wall-clock adjustment
//     cannot make an old reading look new;
//   * a poll that was issued before an action cannot overwrite the action's
//     answer, and a history response is dropped unless it still matches the
//     host, epoch and range it was requested for.

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type {
  AgentHostCapacityHistory,
  AgentRunnerHost,
  AgentRunnerHostPool,
  CapacityRange,
} from '@/lib/api/admin';
import {
  drainAgentRunnerHost,
  getAgentRunnerHostCapacityHistory,
  getAgentRunnerHosts,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  setAgentRuntimeVersion,
  undrainAgentRunnerHost,
} from '@/lib/api/admin';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import CapacityPanel from './CapacityPanel';
import {
  type CapacityCell,
  type HostGroupCapacity,
  cpuCell,
  executionSlotsCell,
  formatUtcTimestamp,
  groupCapacityFacts,
  liveAgeSeconds,
  memoryCell,
  monotonicNow,
  reasonLabel,
} from './capacity';
import { curveSource, curveSourceHosts, formatAge, runtimeMenu, serverInventory } from './format';
import { AdminNavigation } from '../AdminNavigation';

// A runner polls only between jobs, so silence can mean "busy for an hour" as
// easily as "gone". Carried over from the gateway's section along with the
// wording it justifies.
const RECENT_POLL_SECONDS = 300;

// Well under the staleness threshold, so a host that stops polling is reported
// as stale within a poll or two of actually becoming so.
const REFRESH_MS = 30_000;

// The age column advances once a second. The server figure is only refreshed
// every thirty, and an age that steps in thirty-second jumps reads as a frozen
// page on exactly the machine an operator is watching.
const AGE_TICK_MS = 1_000;

const DEFAULT_EXECUTION_PROFILE = 'untrusted-kata-v1';

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

/**
 * A 0-100 reading as a bar under its number.
 *
 * The chart's own axis, at the size of a table cell. A fleet is read down this
 * column, and "8%" and "82%" are the same shape of text; the bar is what makes
 * the difference visible without reading. It is drawn only for a value that was
 * measured — the same rule the number follows — and `aria-hidden`, because the
 * number beside it is the accessible reading and an unlabelled bar is not.
 */
function Meter({ value, muted }: { value: number; muted: boolean }) {
  const width = Math.min(100, Math.max(0, value));
  return (
    <span
      aria-hidden
      className="mt-1.5 block h-1 w-16 overflow-hidden rounded-full bg-gray-200/80 dark:bg-white/10"
    >
      <span
        className={`block h-full rounded-full ${
          muted ? 'bg-gray-400 dark:bg-gray-500' : 'bg-crimson/60 dark:bg-crimson-light/60'
        }`}
        style={{ width: `${width}%` }}
      />
    </span>
  );
}

function CapabilityCell({
  cell,
  muted = false,
  meter = null,
}: {
  cell: CapacityCell;
  muted?: boolean;
  /** The cell's own 0-100 reading, or null when nothing was measured. */
  meter?: number | null;
}) {
  return (
    <div
      // A stale observation keeps its number and loses its confidence: grey is
      // the whole signal that the machine stopped reporting.
      className={muted ? 'text-gray-500 dark:text-gray-400' : 'text-gray-800 dark:text-gray-200'}
      title={cell.title ?? undefined}
    >
      <span className="block whitespace-nowrap tabular-nums">{cell.text}</span>
      {meter !== null && <Meter value={meter} muted={muted} />}
      {cell.note !== null && (
        <span className="mt-0.5 block whitespace-nowrap text-[11px] text-gray-500 dark:text-gray-400">
          {cell.note}
        </span>
      )}
    </div>
  );
}

function RowToggle({
  open,
  name,
  panelId,
  onClick,
}: {
  open: boolean;
  name: string;
  panelId: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={panelId}
      onClick={onClick}
      className="rounded-md border border-gray-200 p-1 text-gray-500 hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson disabled:opacity-40 dark:border-white/10 dark:text-gray-400 dark:hover:bg-white/5 dark:focus-visible:ring-crimson-light"
    >
      <svg
        viewBox="0 0 12 12"
        aria-hidden
        className={`h-3 w-3 transition-transform ${open ? 'rotate-90' : ''}`}
      >
        <path
          d="M4 2l4 4-4 4"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="sr-only">
        {open ? 'Hide capacity for' : 'Show capacity for'} {name}
      </span>
    </button>
  );
}

/**
 * Fill in what a control plane older than this page does not send.
 *
 * The two halves deploy separately on purpose, and the UI may ship first: a
 * control plane without the collector omits `capacity`, `capacity_epoch` and
 * `observed_at` entirely rather than sending nulls. Absent is not the same
 * value as null to the helpers below — `capacity === null` is false for
 * `undefined`, so the type guard that decides "this host was measured" would
 * say yes and then read a field off nothing. Normalizing once, at the funnel
 * every pool response passes through, is what keeps that a single rule instead
 * of a second spelling of "no observation" in every reader.
 */
function normalizeHosts(hosts: AgentRunnerHost[]): AgentRunnerHost[] {
  return hosts.map((host) => ({
    ...host,
    capacity: host.capacity ?? null,
    capacity_epoch: host.capacity_epoch ?? null,
  }));
}

export default function AdminHostsPage() {
  const { state } = useAuth();
  // Keyed on the account: switching users remounts the view, which aborts
  // every request and drops the pool rather than showing one account the
  // other's fleet.
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
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
  return <HostsView key={state.user?.id ?? 'admin'} />;
}

interface PoolSnapshot {
  hosts: AgentRunnerHost[];
  executionProfile: string;
  activeHost: string | null;
  runtimeVersions: Record<string, string>;
  observedAt: string | null;
  /** A monotonic reading taken when this snapshot was applied. */
  appliedAtMs: number;
}

interface HistoryTarget {
  host: string;
  epoch: string | null;
  range: CapacityRange;
}

interface HistorySnapshot {
  key: HistoryTarget;
  data: AgentHostCapacityHistory;
}

function sameTarget(a: HistoryTarget | null, b: HistoryTarget | null): boolean {
  return (
    a !== null && b !== null && a.host === b.host && a.epoch === b.epoch && a.range === b.range
  );
}

function accessDenied(error: unknown): boolean {
  return error instanceof APIError && (error.statusCode === 401 || error.statusCode === 403);
}

function panelIdFor(name: string): string {
  return `capacity-panel-${name.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
}

function HostsView() {
  const [pool, setPool] = useState<PoolSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [denied, setDenied] = useState(false);
  /** A failed background poll: the last good numbers stay, flagged. */
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [nowMs, setNowMs] = useState(monotonicNow);

  const [expanded, setExpanded] = useState<string | null>(null);
  /** Which enrollment's curve the open panel shows; null means the default. */
  const [sourceChoice, setSourceChoice] = useState<string | null>(null);
  const [panelEpoch, setPanelEpoch] = useState<string | null>(null);
  const [range, setRange] = useState<CapacityRange>('1h');
  const [history, setHistory] = useState<HistorySnapshot | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const mounted = useRef(true);
  const poolRequest = useRef<AbortController | null>(null);
  /** The newest poll, so a slow one cannot land on a newer answer. */
  const pollSeq = useRef(0);
  /** Bumped whenever a pool is applied, so an action beats an earlier poll. */
  const appliedSeq = useRef(0);
  const historyRequest = useRef<AbortController | null>(null);
  const historyTargetRef = useRef<HistoryTarget | null>(null);

  // The local half of every age on the page. `performance.now()` cannot be
  // moved by a clock adjustment, which is the property that matters: an
  // operator watching a sick machine must not see its age go backwards.
  useEffect(() => {
    const timer = setInterval(() => setNowMs(monotonicNow()), AGE_TICK_MS);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      poolRequest.current?.abort();
      historyRequest.current?.abort();
    };
  }, []);

  const applyPool = useCallback((next: AgentRunnerHostPool) => {
    const hosts = normalizeHosts(next.hosts);
    const nextEpochs = new Map(hosts.map((host) => [host.host, host.capacity_epoch] as const));
    setPool({
      hosts,
      executionProfile: next.execution_profile ?? DEFAULT_EXECUTION_PROFILE,
      activeHost: next.active_host,
      runtimeVersions: next.runtime_versions ?? {},
      observedAt: next.observed_at,
      appliedAtMs: monotonicNow(),
    });
    // A rotated epoch is a different machine behind the same name: whatever is
    // cached for the old one describes hardware that is no longer there.
    setHistory((current) => {
      if (current === null || !nextEpochs.has(current.key.host)) return current;
      return nextEpochs.get(current.key.host) === current.key.epoch ? current : null;
    });
  }, []);

  const clearAll = useCallback(() => {
    poolRequest.current?.abort();
    historyRequest.current?.abort();
    pollSeq.current += 1;
    historyTargetRef.current = null;
    setDenied(true);
    setPool(null);
    setHistory(null);
    setHistoryError(null);
    setHistoryLoading(false);
    setLoading(false);
    setBusy(false);
    setNotice(null);
    setError(null);
    setRefreshFailed(false);
    setExpanded(null);
  }, []);

  const load = useCallback(
    async (background = false) => {
      if (!background) setLoading(true);
      setError(null);
      poolRequest.current?.abort();
      const controller = new AbortController();
      poolRequest.current = controller;
      const seq = (pollSeq.current += 1);
      // A poll issued before an action must not land on top of its answer: the
      // action is a fresh read of the same pool and is always the newer fact.
      const applied = appliedSeq.current;
      try {
        const next = await getAgentRunnerHosts(controller.signal);
        if (!mounted.current || controller.signal.aborted) return;
        if (seq !== pollSeq.current || applied !== appliedSeq.current) return;
        applyPool(next);
        appliedSeq.current += 1;
        setRefreshFailed(false);
      } catch (e) {
        if (!mounted.current || controller.signal.aborted) return;
        if (accessDenied(e)) {
          clearAll();
          return;
        }
        // A failed background poll leaves the last good pool on screen rather
        // than replacing it with an error: the numbers going stale is a
        // smaller lie than the page claiming there is nothing there. The
        // indication matters, though — unlabelled stale numbers are the lie.
        if (background) setRefreshFailed(true);
        else setError(getErrorMessage(e));
      } finally {
        if (mounted.current && seq === pollSeq.current && !background) setLoading(false);
      }
    },
    [applyPool, clearAll],
  );

  const loadHistory = useCallback(
    async (background = false) => {
      const requested = historyTargetRef.current;
      if (requested === null) return;
      historyRequest.current?.abort();
      const controller = new AbortController();
      historyRequest.current = controller;
      if (!background) setHistoryLoading(true);
      setHistoryError(null);
      try {
        const data = await getAgentRunnerHostCapacityHistory(
          requested.host,
          requested.range,
          controller.signal,
        );
        if (!mounted.current || controller.signal.aborted) return;
        // Tagged with what it was requested for. A late response that no
        // longer matches the open panel — another server, another range, or a
        // re-enrollment — is discarded rather than drawn under a new title.
        const current = historyTargetRef.current;
        if (
          !sameTarget(current, requested) ||
          data.host !== requested.host ||
          data.capacity_epoch !== requested.epoch
        ) {
          return;
        }
        setHistory({ key: requested, data });
      } catch (e) {
        if (!mounted.current || controller.signal.aborted) return;
        if (accessDenied(e)) {
          clearAll();
          return;
        }
        setHistoryError(getErrorMessage(e));
      } finally {
        if (mounted.current) setHistoryLoading(false);
      }
    },
    [clearAll],
  );

  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      if (document.hidden) return;
      void load(true);
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  // Everything here is time-relative — poll ages, and the warning that fires
  // when the pinned host goes quiet. Fetched once, the ages freeze at whatever
  // they were when the tab was opened and the warning can never appear for a
  // machine that dies afterwards, which is exactly when an operator is looking.
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden) return;
      void load(true);
      void loadHistory(true);
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [load, loadHistory]);

  const executionProfile = pool?.executionProfile ?? DEFAULT_EXECUTION_PROFILE;
  const runtimeVersions = pool?.runtimeVersions ?? {};
  // Memoised so the inventory below is not rebuilt on every age tick: the
  // pool object only changes when a response lands.
  const hosts = useMemo(() => pool?.hosts ?? [], [pool]);
  const servers = useMemo(
    () => serverInventory(hosts, executionProfile),
    [hosts, executionProfile],
  );
  const availableServers = servers.filter((server) => server.available);
  const visibleServers = servers.filter(
    (server) => server.host && server.host.status !== 'disabled',
  );
  const inactiveServers = servers.filter(
    (server) => !server.host || server.host.status === 'disabled',
  );
  const activeHost = pool?.activeHost ?? null;
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
  const elapsedMs = pool === null ? 0 : Math.max(0, nowMs - pool.appliedAtMs);

  const openServer = expanded === null ? null : (servers.find((s) => s.name === expanded) ?? null);
  const openFacts = openServer === null ? null : groupCapacityFacts(openServer.members, elapsedMs);
  // Every live enrollment behind this physical name is a candidate curve,
  // because a server that reports as one row can be several deployments and
  // only the operator knows which one they came to look at. A removed one is
  // not: its row survives for the attempts and history that name it, but it is
  // not an environment to read a curve from. The default is the source the
  // row's own CPU and memory came from, so the panel opens on the machine the
  // numbers above it describe.
  const openMembers = openServer === null ? [] : curveSourceHosts(openServer.members);
  const openSources = openMembers.map((member) => member.host);
  const chosenSource =
    sourceChoice === null
      ? null
      : (openMembers.find((member) => member.host === sourceChoice) ?? null);
  const openSource =
    chosenSource ??
    curveSource(
      openServer?.members ?? [],
      openFacts?.source?.host ?? null,
      openServer?.host?.host ?? null,
    );
  // The enrollment whose history the panel reads, named by the three things a
  // response has to match to be drawn: a curve is only ever shown under the
  // host, generation and window it was requested for.
  const historyHost = openServer === null ? null : (openSource?.host ?? null);
  const historyEpoch = openServer === null ? null : (openSource?.capacity_epoch ?? null);
  const historyTarget: HistoryTarget | null =
    historyHost === null ? null : { host: historyHost, epoch: historyEpoch, range };
  const historyTargetKey =
    historyHost === null ? null : `${historyHost}\u0000${historyEpoch ?? ''}\u0000${range}`;

  useEffect(() => {
    historyTargetRef.current =
      historyHost === null ? null : { host: historyHost, epoch: historyEpoch, range };
    // Closing the panel stops its polling and drops its curve; the control
    // plane keeps sampling regardless.
    if (historyHost === null) {
      historyRequest.current?.abort();
      setHistory(null);
      setHistoryError(null);
      setHistoryLoading(false);
      return;
    }
    void loadHistory();
  }, [historyHost, historyEpoch, range, loadHistory]);

  useEffect(() => {
    if (historyTargetKey === null) return;
    const timer = setInterval(() => {
      if (document.hidden) return;
      void loadHistory(true);
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [historyTargetKey, loadHistory]);

  // Every action returns the whole pool, so they all end the same way.
  const run = useCallback(
    async (action: () => Promise<AgentRunnerHostPool>, ok: string) => {
      setBusy(true);
      setNotice(null);
      try {
        const next = await action();
        if (!mounted.current) return;
        // The action's snapshot is newer than any poll already in flight.
        pollSeq.current += 1;
        appliedSeq.current += 1;
        applyPool(next);
        setRefreshFailed(false);
        setNotice(ok);
      } catch (e) {
        if (!mounted.current) return;
        if (accessDenied(e)) {
          clearAll();
          return;
        }
        setNotice(getErrorMessage(e));
      } finally {
        if (mounted.current) setBusy(false);
      }
    },
    [applyPool, clearAll],
  );

  const handlePin = (host: string | null) =>
    run(
      () => setActiveAgentRunnerHost(host),
      host
        ? `New chats now go to ${serverInventory(hosts, executionProfile).find((server) => server.host?.host === host)?.name ?? host}. Existing retained chats stay on their owning hosts.`
        : 'New chats are assigned automatically. Existing retained chats stay on their owning hosts.',
    );

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

  const toggleServer = (server: string, facts: HostGroupCapacity) => {
    if (expanded === server) {
      setExpanded(null);
      setSourceChoice(null);
      return;
    }
    setExpanded(server);
    // **Pinned here, to the source the row is showing.** Left null, the
    // default would be recomputed on every poll and the curve would follow
    // whichever enrollment happened to report most recently — two
    // environments alternating their beats would swap the chart under the
    // operator, and the epoch check would call the swap a re-enrollment.
    // Opening the panel is the moment the choice is made; only the operator
    // changes it afterwards.
    //
    // The fallback is pinned too, and it has to be: when nothing has been
    // measured yet the panel opens on the representative enrollment, and
    // leaving the choice null would let the first enrollment to report — a
    // different machine — take the chart without anybody asking.
    const group = servers.find((item) => item.name === server);
    const opened = curveSource(
      group?.members ?? [],
      facts.source?.host ?? null,
      group?.host?.host ?? null,
    );
    setSourceChoice(opened?.host ?? null);
    // Recorded so a re-enrollment while the panel is open can be named as
    // such, rather than looking like a curve that lost its history.
    setPanelEpoch(opened?.capacity_epoch ?? null);
    setHistory(null);
    setHistoryError(null);
  };

  /**
   * Follow the operator's choice of source, and its generation with it.
   *
   * The two move together: the restart note is a comparison between the
   * generation on screen and the one the panel opened on, so a switch that
   * changed only the host would read as the new host having re-enrolled. The
   * operator asked to look at another deployment on this machine, not to be
   * told the machine was rebuilt.
   */
  const selectSource = (host: string) => {
    const member = openServer?.members.find((item) => item.host === host) ?? null;
    setSourceChoice(host);
    setPanelEpoch(member?.capacity_epoch ?? null);
  };

  if (denied) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
          Admin access required
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Your access to the runner host pool was revoked.
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
          denser dialect, which read as unfinished beside every other screen.
          Wide, because the capacity columns are what an operator compares
          across a fleet and two of them do not fit a column of prose. */}
      <section className="mx-auto w-full max-w-7xl px-6 py-12 lg:px-12 lg:py-16">
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
        <AdminNavigation current="servers" />

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
            {staleActive.host} last polled{' '}
            {formatAge(liveAgeSeconds(staleActive.seconds_since_seen, elapsedMs))}. New chats may be
            unavailable. Existing retained chats stay on their owning server.
          </div>
        )}

        {/* Physical inventory groups independently enrolled execution environments.
            Actions still target the original deployment identity. */}
        <div className="mt-10">
          <div className="flex flex-wrap items-baseline gap-x-3">
            <h2 className="text-base font-semibold text-gray-900 dark:text-gray-50">Servers</h2>
            {refreshFailed && (
              <span
                aria-live="polite"
                className="ml-auto text-[11px] text-amber-700 dark:text-amber-300"
              >
                Refresh failed — showing the last successful read
                {pool?.observedAt ? ` from ${formatUtcTimestamp(pool.observedAt)}` : ''}.
              </span>
            )}
          </div>

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

          {loading && pool === null ? (
            <TableSkeleton />
          ) : visibleServers.length === 0 ? (
            <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">No servers available.</p>
          ) : (
            <div className="mt-4 overflow-x-auto rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 shadow-sm">
              <table className="min-w-[1080px] w-full text-left text-sm">
                <thead className="border-b border-gray-100 bg-gray-50/60 text-xs text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
                  <tr>
                    <th className="px-6 py-3 font-medium">Server</th>
                    <th className="px-3 py-3 font-medium">Status</th>
                    <th className="px-3 py-3 font-medium whitespace-nowrap">Execution slots</th>
                    <th className="px-3 py-3 font-medium">CPU</th>
                    <th className="px-3 py-3 font-medium">Memory</th>
                    <th className="px-3 py-3 font-medium whitespace-nowrap">Sandboxes</th>
                    <th className="px-3 py-3 font-medium whitespace-nowrap">Observed</th>
                    <th className="px-6 py-3 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                  {visibleServers.map(({ name, host, members }) => {
                    if (!host) return null;
                    const facts = groupCapacityFacts(members, elapsedMs);
                    const ageSeconds =
                      facts.oldestSampleAgeSeconds === null
                        ? null
                        : liveAgeSeconds(facts.oldestSampleAgeSeconds, elapsedMs);
                    const open = expanded === name;
                    const panelId = panelIdFor(name);
                    const sandboxTitle = [
                      facts.sandboxes.explanation,
                      ...facts.sandboxes.reasons.map(reasonLabel),
                    ]
                      .filter((part): part is string => Boolean(part))
                      .join(' ');
                    return (
                      <Fragment key={name}>
                        <tr
                          className="cursor-pointer align-top hover:bg-gray-50/60 dark:hover:bg-white/[0.03]"
                          onClick={(event) => {
                            // Buttons inside the row keep their own meaning; a
                            // row click is a convenience for the toggle, not a
                            // second way to press Drain.
                            if ((event.target as HTMLElement).closest('button, a, select, input')) {
                              return;
                            }
                            toggleServer(name, facts);
                          }}
                        >
                          <th
                            scope="row"
                            aria-label={name}
                            className="px-6 py-4 font-medium text-gray-900 dark:text-gray-50"
                          >
                            <div className="flex items-center gap-2">
                              {/* The row header keeps the server's own name:
                                  the toggle speaks for itself inside it, so a
                                  reader asking "which machine is this row?"
                                  is not answered with a button's label. */}
                              <RowToggle
                                open={open}
                                name={name}
                                panelId={panelId}
                                onClick={() => toggleServer(name, facts)}
                              />
                              <span>{name}</span>
                            </div>
                          </th>
                          <td className="px-3 py-4">
                            <StatusBadge status={host.status} />
                          </td>
                          <td className="px-3 py-4">
                            <CapabilityCell cell={executionSlotsCell(host)} muted={facts.stale} />
                          </td>
                          <td className="px-3 py-4">
                            <CapabilityCell
                              cell={cpuCell(facts.capacity)}
                              muted={facts.stale}
                              meter={facts.capacity?.cpu_percent ?? null}
                            />
                          </td>
                          <td className="px-3 py-4">
                            <CapabilityCell
                              cell={memoryCell(facts.capacity)}
                              muted={facts.stale}
                              meter={facts.capacity?.memory_used_percent ?? null}
                            />
                          </td>
                          <td className="px-3 py-4" title={sandboxTitle || undefined}>
                            <span
                              className={`block whitespace-nowrap ${
                                facts.sandboxes.staleOnly
                                  ? 'text-amber-700 dark:text-amber-300'
                                  : 'text-gray-800 dark:text-gray-200'
                              }`}
                            >
                              {facts.sandboxes.text}
                            </span>
                            {facts.sandboxes.partial && (
                              <span className="mt-0.5 block text-[11px] text-amber-700 dark:text-amber-300">
                                {facts.sandboxes.reasons.length > 0
                                  ? facts.sandboxes.reasons.map(reasonLabel).join(' · ')
                                  : 'Partial coverage'}
                              </span>
                            )}
                          </td>
                          <td className="px-3 py-4 whitespace-nowrap tabular-nums">
                            {ageSeconds === null ? (
                              <span className="text-gray-500 dark:text-gray-400">Not measured</span>
                            ) : (
                              <span
                                className={
                                  facts.stale
                                    ? 'text-amber-700 dark:text-amber-300'
                                    : 'text-gray-800 dark:text-gray-200'
                                }
                              >
                                {formatAge(ageSeconds)}
                              </span>
                            )}
                            {facts.stale && (
                              <span className="mt-1 block text-[11px] font-medium text-amber-700 dark:text-amber-300">
                                stale
                              </span>
                            )}
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
                                className="rounded-lg border border-gray-200 bg-white px-2.5 py-1 text-xs font-medium text-gray-700 transition-colors hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson disabled:opacity-40 dark:border-white/10 dark:bg-transparent dark:text-gray-200 dark:hover:bg-white/5 dark:focus-visible:ring-crimson-light"
                              >
                                {host.status === 'draining' ? 'Undrain' : 'Drain'}
                              </button>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => handleRemove(host.host, host.slots_used)}
                                className="rounded-lg border border-transparent px-2.5 py-1 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 disabled:opacity-40 dark:text-red-400 dark:hover:bg-red-500/10"
                              >
                                Remove
                              </button>
                            </div>
                          </td>
                        </tr>
                        {open && openServer && openFacts && (
                          // No second card inside this one: the band is the
                          // detail surface, and the panel draws on it directly
                          // at the table's own first-column padding.
                          <tr className="bg-gray-50/70 dark:bg-white/[0.03]">
                            <td colSpan={8} className="px-6 pb-6 pt-4">
                              <CapacityPanel
                                id={panelId}
                                name={name}
                                source={openSource?.host ?? null}
                                sources={openSources}
                                onSourceChange={selectSource}
                                sandboxes={openFacts.sandboxes}
                                oldestAgeSeconds={
                                  openFacts.oldestSampleAgeSeconds === null
                                    ? null
                                    : liveAgeSeconds(openFacts.oldestSampleAgeSeconds, elapsedMs)
                                }
                                stale={openFacts.stale}
                                range={range}
                                onRangeChange={setRange}
                                history={
                                  sameTarget(history?.key ?? null, historyTarget)
                                    ? (history?.data ?? null)
                                    : null
                                }
                                loading={historyLoading}
                                error={historyError}
                                onRetry={() => void loadHistory()}
                                restarted={
                                  panelEpoch !== null &&
                                  openSource?.capacity_epoch != null &&
                                  openSource.capacity_epoch !== panelEpoch
                                }
                              />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
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
        </div>

        {/* ── Harness version ──────────────────────────────────────────
            One row per runtime, because the previous inline strip put five
            selects and five bare runtime names on a single line: five copies
            of "Image default" with nothing to say which was which, and no
            room to explain that the options come from the image.

            Two layers the wording has to keep apart: the *menu* is built into
            each sandbox image and changes only by rebuilding; this picks which
            item on it new work runs. A runtime with one option is an image
            holding one version — add more through that host's
            `*_EXTRA_VERSIONS`.

            Below the table: capacity is what an operator opens this page for,
            and a configuration control above it pushed the fleet below the
            fold. */}
        {menu.length > 0 && (
          <div className="mt-10">
            {/* No subtitle, unlike the section above, because both of the
                things one would say are already said where they land: that a
                running conversation keeps its version is in the confirmation
                after the change, and where the options come from — and how to
                add one — is deployment documentation, in `.env.example` and
                ENVIRONMENT.md, read by the person editing the `.env` rather
                than by anyone standing on this page. */}
            <h2 className="text-base font-semibold text-gray-900 dark:text-gray-50">
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
      </section>
    </div>
  );
}

/** Two placeholder rows in the table's own shape, not a spinner over it. */
function TableSkeleton() {
  return (
    <div className="mt-4 overflow-hidden rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 shadow-sm">
      <table className="min-w-[1080px] w-full text-left text-sm">
        <thead className="border-b border-gray-100 dark:border-white/5 text-xs text-gray-500 dark:text-gray-400">
          <tr>
            <th className="px-6 py-3 font-medium">Server</th>
            <th className="px-3 py-3 font-medium">Status</th>
            <th className="px-3 py-3 font-medium whitespace-nowrap">Execution slots</th>
            <th className="px-3 py-3 font-medium">CPU</th>
            <th className="px-3 py-3 font-medium">Memory</th>
            <th className="px-3 py-3 font-medium whitespace-nowrap">Sandboxes</th>
            <th className="px-3 py-3 font-medium whitespace-nowrap">Observed</th>
            <th className="px-6 py-3 text-right font-medium">Actions</th>
          </tr>
        </thead>
        <tbody aria-hidden className="divide-y divide-gray-100 dark:divide-white/5">
          {[0, 1].map((row) => (
            <tr key={row}>
              <td className="px-6 py-4">
                <div className="h-4 w-28 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
              </td>
              <td className="px-3 py-4">
                <div className="h-5 w-16 animate-pulse rounded-full bg-gray-100 dark:bg-white/5" />
              </td>
              {[0, 1, 2, 3, 4, 5].map((cell) => (
                <td key={cell} className="px-3 py-4">
                  <div className="h-4 w-20 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="sr-only">Loading servers</p>
    </div>
  );
}
