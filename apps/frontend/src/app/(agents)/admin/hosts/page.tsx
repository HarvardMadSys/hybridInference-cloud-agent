'use client';

// Host admission and capacity, alongside the independent compute resource lifecycle.
//
// Runners pull work, so nothing here moves a sandbox to a machine. The pin
// chooses where chats without a sandbox may be materialized; an existing
// retained chat remains claimable only by its Docker-owning host. "Pinned",
// not "assigned"; "last polled", not "online".
//
// ── Capacity ──────────────────────────────────────────────────────────
//
// Each host now also reports what its Docker host is actually doing: CPU,
// memory, storage and how many sandboxes are retained. Three rules govern how
// it is shown, and each is the difference between a number and a claim:
//
//   * a missing measurement is never rendered as zero, and a stale one is
//     never rendered as current;
//   * staleness advances by local monotonic time — the server's figure plus
//     `performance.now()` deltas — so a failed poll eventually marks the last
//     good numbers stale, and a wall-clock adjustment cannot make an old
//     reading look new;
//   * a poll that was issued before an action cannot overwrite the action's
//     answer, and a history response is dropped unless it still matches the
//     host, epoch and range it was requested for.
//
// Storage is the third indicator on the row and the same kind of fact as
// memory: one instantaneous observation of the filesystem the sandboxes write
// to, taken alongside the CPU and memory in the same 30-second sample. It gets
// no curve of its own — a disk fills over days, and the minutes-long history
// the panel already holds would show noise — and its cell therefore reads the
// observation `groupCapacityFacts` picked for CPU and memory, never a source
// of its own.

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type {
  AgentHostCapacityHistory,
  AgentRunnerHost,
  AgentRunnerHostPool,
  CapacityRange,
  HostSettingDefinition,
  HostSettingValues,
} from '@/lib/api/admin';
import { MAX_HOST_SLOTS, MIN_HOST_SLOTS } from '@/lib/api/admin';
import {
  forgetAgentRunnerHost,
  getAgentRunnerHostCapacityHistory,
  getAgentRunnerHosts,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  setAgentRunnerHostSettings,
  setAgentRunnerHostSlots,
  undrainAgentRunnerHost,
} from '@/lib/api/admin';
import type { ComputeServerSnapshot } from '@/lib/api/compute';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import CapacityPanel from './CapacityPanel';
import ComputeResources from './ComputeResources';
import { ServerSettingsPanel } from './HostSettings';
import hostStyles from './host-controls.module.css';
import {
  type CapacityCell,
  type HostGroupCapacity,
  concurrencyFacts,
  cpuCell,
  executionSlotsCell,
  formatUtcTimestamp,
  groupCapacityFacts,
  liveAgeSeconds,
  memoryCell,
  monotonicNow,
  reasonLabel,
  storageCell,
} from './capacity';
import {
  curveSource,
  curveSourceHosts,
  formatAge,
  hostStatusLabel,
  serverInventory,
} from './format';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';

// A runner polls only between jobs, so silence can mean "busy for an hour" as
// easily as "gone". Carried over from the gateway's section along with the
// wording it justifies.
const RECENT_POLL_SECONDS = 300;

// Well under the staleness threshold, so a host that stops polling is reported
// as stale within a poll or two of actually becoming so.
const REFRESH_MS = 30_000;

// Re-evaluate staleness once a second so a reading crosses the freshness
// threshold promptly even when refreshes are failing.
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

function StatusBadge({ host }: { host: AgentRunnerHost }) {
  const style = STATUS_STYLES[host.status] ?? STATUS_STYLES.disabled;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${style}`}
    >
      {hostStatusLabel(host)}
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
      // A stale observation keeps its number and loses its confidence; the
      // muted value reinforces the explicit warning under Status.
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
      aria-label={`${open ? 'Hide details for' : 'Show details for'} ${name}`}
      aria-controls={panelId}
      onClick={onClick}
      className="ui-button ui-button-secondary min-h-9 gap-1.5 px-3 py-1.5 text-xs [@media(pointer:coarse)]:min-h-11"
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
      {/* The band holds the server's usage history and its runtime settings,
          so the toggle names neither one. */}
      <span aria-hidden="true">Details</span>
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
    // The requested executor count arrived later still, and "never sent"
    // must read as "not set" rather than leave the cell and its control to
    // disagree.
    slots_desired: host.slots_desired ?? null,
    // Runtime settings arrived last of all. Nothing set and nothing reported
    // are the honest readings of a control plane that sends neither.
    settings_desired: host.settings_desired ?? {},
    settings_applied: host.settings_applied ?? null,
    settings_local: host.settings_local ?? null,
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
  /** Runtime settings set for every server. */
  hostSettings: HostSettingValues;
  /** Every runtime setting an admin may set; empty from an older control plane. */
  hostSettingDefinitions: HostSettingDefinition[];
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

// Why a removal would leave sandboxes on the host, by the server's refusal.
const UNMOVED_REASONS: Record<string, (host: string) => string> = {
  no_move_target: (host) =>
    `No other server can take the sandboxes on ${host} now, or checkpoints are off.`,
  cannot_move: (host) =>
    `Some sandboxes on ${host} have no checkpoint another server can restore, for example ` +
    'because a checkpoint failed, so they cannot move. The others still move first.',
};

// What a moving removal still waits for. Sandboxes that cannot move hold it
// until the administrator agrees to leave them, by removing again.
function movingOutProgress(host: AgentRunnerHost): string {
  const progress = `Moving out: ${host.sandboxes_left ?? 0} sandbox(es), ${host.slots_used} running turn(s) left`;
  const unmovable = host.sandboxes_unmovable ?? 0;
  if (unmovable === 0) return progress;
  return host.removal_leaves_unmoved
    ? `${progress}; ${unmovable} cannot move and stay`
    : `${progress}; ${unmovable} cannot move, remove again to leave them`;
}

function HostsView() {
  const [computeInventory, setComputeInventory] = useState<ComputeServerSnapshot | null>(null);
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
      hostSettings: next.host_settings ?? {},
      hostSettingDefinitions: next.host_setting_definitions ?? [],
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
  const hostSettings = pool?.hostSettings ?? {};
  const settingDefinitions = pool?.hostSettingDefinitions ?? [];
  // Memoised so the inventory below is not rebuilt on every age tick: the
  // pool object only changes when a response lands.
  const hosts = useMemo(() => pool?.hosts ?? [], [pool]);
  const servers = useMemo(
    () => serverInventory(hosts, executionProfile),
    [hosts, executionProfile],
  );
  const availableHosts = hosts.filter(
    (host) =>
      computeInventory?.hosts.find((resource) => resource.host === host.host)?.allows_new_groups !==
        false &&
      host.enrolled &&
      host.status === 'online' &&
      (executionProfile === 'trusted-v1' || host.sandbox_runtime === 'io.containerd.kata.v2'),
  );
  const activeHost = pool?.activeHost ?? null;
  const selectionUnavailable = Boolean(
    activeHost &&
    !hosts.some(
      (host) =>
        host.host === activeHost &&
        computeInventory?.hosts.find((resource) => resource.host === host.host)
          ?.allows_new_groups !== false &&
        host.enrolled &&
        host.status === 'online' &&
        (executionProfile === 'trusted-v1' || host.sandbox_runtime === 'io.containerd.kata.v2'),
    ),
  );
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

  // Every action returns the whole pool, so they all end the same way. An
  // action whose effect is visible on the row it changed passes no notice:
  // the row is the confirmation, and a banner restating it is noise.
  const run = useCallback(
    async (
      action: () => Promise<AgentRunnerHostPool>,
      ok: string | null | ((pool: AgentRunnerHostPool) => string | null),
    ) => {
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
        setNotice(typeof ok === 'function' ? ok(next) : ok);
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

  // No notice: the number lands in the row's own cell, which says "applying"
  // until the host confirms it and then shows it in force. Whether the host
  // has taken it up is on the row, where it keeps being true.
  const handleSlots = (host: string, slots: number) =>
    run(() => setAgentRunnerHostSlots(host, slots), null);

  // No notice either: each setting's own line says how far a saved value has
  // reached, and keeps saying it as the servers take it up.
  const handleServerSetting = (host: string, key: string, value: number | null) =>
    void run(() => setAgentRunnerHostSettings(host, { [key]: value }), null);

  // A busy host is not cut off: it stops taking new work, and the control
  // plane removes it once its running turns end. The row says "removing" until
  // then, so only the idle case needs a notice.
  // Its sandboxes move to other servers first when they can. When some would
  // stay on it, the server says why, and removing anyway is the
  // administrator's decision.
  const handleRemove = (host: string) =>
    void run(
      async () => {
        try {
          return await removeAgentRunnerHost(host, false);
        } catch (e) {
          if (!(e instanceof APIError)) throw e;
          const why = UNMOVED_REASONS[e.code];
          if (why === undefined) throw e;
          const anyway = window.confirm(
            `${why(host)} Removing it anyway leaves them on it, and their chats cannot ` +
              'continue until it returns. Remove anyway?',
          );
          return anyway ? removeAgentRunnerHost(host, false, true) : getAgentRunnerHosts();
        }
      },
      (pool) => {
        const after = pool.hosts.find((item) => item.host === host);
        if (!after?.removal_pending) {
          return after?.status === 'disabled' ? `Removed ${host} from the pool.` : null;
        }
        return after.removal_moves_out
          ? `${host} stops taking new work, moves its sandboxes to other servers, and is removed once they have moved.`
          : `${host} stops taking new work and is removed when its ${after.slots_used} running turn(s) finish.`;
      },
    );

  // The way out of waiting, with the consequence stated before the request.
  const handleRemoveNow = (host: string, slotsUsed: number, sandboxesLeft: number) => {
    const ok = window.confirm(
      `${host} is still running ${slotsUsed} attempt(s) and holds ${sandboxesLeft} sandbox(es) ` +
        'not yet moved. Removing it now ends the attempts: their leases expire now, and the ' +
        'work is requeued once, or a chat whose turn may already have run is marked lost. ' +
        'Sandboxes not yet moved stay on it. Remove now?',
    );
    if (!ok) return;
    void run(() => removeAgentRunnerHost(host, true), `Removed ${host} from the pool.`);
  };

  // A removal still waiting on turns or sandboxes offers to force it instead.
  const handleRemoveClick = (host: AgentRunnerHost) => {
    const sandboxesLeft = host.sandboxes_left ?? 0;
    if (host.removal_pending && (host.slots_used > 0 || sandboxesLeft > 0)) {
      handleRemoveNow(host.host, host.slots_used, sandboxesLeft);
    } else {
      handleRemove(host.host);
    }
  };

  const handleForget = (host: string) => {
    if (
      !window.confirm(
        `Forget the management record for ${host}? This leaves the machine and its files intact. ` +
          'Retained sandboxes, running work, placement references, or managed cloud resources block this action.',
      )
    )
      return;
    void run(
      () => forgetAgentRunnerHost(host),
      `Forgot ${host}. The machine and its files were not deleted.`,
    );
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

  const renderHostControls = (hostIds: string[]) => {
    const scopedHosts = hosts.filter((host) => hostIds.includes(host.host));
    const scopedServers = serverInventory(scopedHosts, executionProfile);
    const scopedVisible = scopedServers.filter(
      (server) => server.host && server.host.status !== 'disabled',
    );
    const scopedInactive = scopedServers.filter(
      (server) => !server.host || server.host.status === 'disabled',
    );
    return (
      <div className={hostStyles.controls}>
        {refreshFailed && (
          <p aria-live="polite" className="mt-3 text-xs text-amber-700 dark:text-amber-300">
            Refresh failed — showing the last successful read
            {pool?.observedAt ? ` from ${formatUtcTimestamp(pool.observedAt)}` : ''}.
          </p>
        )}
        {loading && pool === null ? (
          <TableSkeleton />
        ) : scopedVisible.length === 0 ? (
          <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">No servers available.</p>
        ) : (
          <div className="mt-4 overflow-x-auto rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 shadow-sm">
            <table className={`${hostStyles.table} w-full text-left text-sm`}>
              <thead className="border-b border-gray-100 bg-gray-50/60 text-xs text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
                <tr>
                  <th className="px-6 py-3 font-medium">Server</th>
                  <th className="px-3 py-3 font-medium">Status</th>
                  <th className="px-3 py-3 font-medium whitespace-nowrap">Execution slots</th>
                  <th className="px-3 py-3 font-medium">CPU</th>
                  <th className="px-3 py-3 font-medium">Memory</th>
                  <th className="px-3 py-3 font-medium">Storage</th>
                  <th className="px-3 py-3 font-medium whitespace-nowrap">Sandboxes</th>
                  <th className="px-6 py-3 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                {scopedVisible.map(({ name, host, members }) => {
                  if (!host) return null;
                  const facts = groupCapacityFacts(members, elapsedMs);
                  const open = expanded === name;
                  const panelId = panelIdFor(name);
                  const concurrency = concurrencyFacts(host);
                  const allocationId = computeInventory?.hosts.find(
                    (item) => item.host === host.host,
                  )?.allocation_id;
                  const autoscalingManaged =
                    computeInventory?.allocations
                      .find((allocation) => allocation.id === allocationId)
                      ?.blocked_reasons.some((reason) => reason.code === 'autoscaling_managed') ??
                    false;
                  const sandboxTitle = [
                    facts.sandboxes.explanation,
                    ...facts.sandboxes.reasons.map(reasonLabel),
                  ]
                    .filter((part): part is string => Boolean(part))
                    .join(' ');
                  return (
                    <Fragment key={name}>
                      <tr
                        className={hostStyles.hostRow}
                        onClick={(event) => {
                          // Buttons inside the row keep their own meaning; a
                          // row click is a convenience for the toggle, not a
                          // second way to press Remove.
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
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <span>{name}</span>
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
                          </div>
                        </th>
                        <td data-label="Status" className="px-3 py-4">
                          <StatusBadge host={host} />
                          {host.removal_pending && (
                            <span className="mt-1 block whitespace-nowrap text-[11px] text-gray-500 dark:text-gray-400">
                              {host.removal_moves_out
                                ? movingOutProgress(host)
                                : `${host.slots_used} running turn(s) left`}
                            </span>
                          )}
                          {facts.stale && (
                            <span className="mt-1 block whitespace-nowrap text-[11px] font-medium text-amber-700 dark:text-amber-300">
                              Capacity stale
                            </span>
                          )}
                        </td>
                        <td data-label="Concurrent turns" className="px-3 py-4">
                          <CapabilityCell cell={executionSlotsCell(host)} muted={facts.stale} />
                          {/* The control sits under the number it changes.
                                It asks for a limit, not a fleet size: the host
                                starts a process per turn as it claims one, so
                                the input is bounded only by the protocol's
                                floor and a typo guard, and no machine capacity
                                is implied by either.

                                The field is uncontrolled and re-keyed on the
                                saved value, so a poll landing mid-edit does not
                                overwrite what the operator is typing, and a
                                saved change does replace it.

                                Saving is not applying, and the cell's own note
                                says which happened — "applying 8" until the
                                host confirms, "not applied" for an offline
                                host, "redeploy to change" for a runner that
                                predates remote limits — with the longer reason
                                on hover. Nothing else is written under the
                                field: the number is the host's number, and an
                                operator who wants a different one types it. */}
                          {autoscalingManaged ? (
                            <p className="mt-2 max-w-[15rem] text-[11px] text-gray-500 dark:text-gray-400">
                              Slots are managed from fresh capacity by CloudLab auto-scaling.
                            </p>
                          ) : (
                            <form
                              className="mt-2 flex min-w-0 max-w-full flex-wrap items-center gap-2"
                              onSubmit={(event) => {
                                event.preventDefault();
                                const value = Number(
                                  new FormData(event.currentTarget).get('slots'),
                                );
                                if (
                                  !Number.isInteger(value) ||
                                  value < MIN_HOST_SLOTS ||
                                  value > MAX_HOST_SLOTS
                                ) {
                                  return;
                                }
                                void handleSlots(host.host, value);
                              }}
                            >
                              <input
                                key={`${host.host}:${concurrency.requested ?? 'default'}`}
                                name="slots"
                                type="number"
                                min={MIN_HOST_SLOTS}
                                max={MAX_HOST_SLOTS}
                                step={1}
                                defaultValue={
                                  concurrency.requested ?? host.slots_total ?? MIN_HOST_SLOTS
                                }
                                aria-label={`Turns at once for ${name}`}
                                disabled={busy}
                                className="ui-input min-h-9 w-20 min-w-0 max-w-full px-2.5 py-1.5 tabular-nums [@media(pointer:coarse)]:min-h-11"
                              />
                              <button
                                type="submit"
                                disabled={busy}
                                className="ui-button ui-button-secondary min-h-9 px-3 py-1.5 text-xs [@media(pointer:coarse)]:min-h-11"
                              >
                                Apply
                              </button>
                            </form>
                          )}
                          {concurrency.launchError !== null && (
                            <span
                              role="alert"
                              className="mt-1 block max-w-[15rem] text-[11px] text-red-600 dark:text-red-400"
                            >
                              {concurrency.launchError}
                            </span>
                          )}
                        </td>
                        <td data-label="CPU" className="px-3 py-4">
                          <CapabilityCell
                            cell={cpuCell(facts.capacity)}
                            muted={facts.stale}
                            meter={facts.capacity?.cpu_percent ?? null}
                          />
                        </td>
                        <td data-label="Memory" className="px-3 py-4">
                          <CapabilityCell
                            cell={memoryCell(facts.capacity)}
                            muted={facts.stale}
                            meter={facts.capacity?.memory_used_percent ?? null}
                          />
                        </td>
                        <td data-label="Storage" className="px-3 py-4">
                          <CapabilityCell
                            cell={storageCell(facts.capacity)}
                            muted={facts.stale}
                            meter={facts.capacity?.storage_used_percent ?? null}
                          />
                          {/* A recommendation, not a refusal: this host takes
                                untrusted work and gives each new sandbox an
                                unbounded writable layer on the disk above. */}
                          {host.untrusted_without_disk_quota === true && (
                            <span
                              role="status"
                              title="This server runs untrusted sandboxes with no container disk quota, so one sandbox can fill the disk the others write to. Set Container disk above 0 (needs Docker overlay2 on XFS with project quotas)."
                              className="mt-1 block max-w-[15rem] text-[11px] font-medium text-amber-700 dark:text-amber-300"
                            >
                              No container disk quota
                            </span>
                          )}
                        </td>
                        <td
                          data-label="Sandboxes"
                          className="px-3 py-4"
                          title={sandboxTitle || undefined}
                        >
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
                        <td className={`${hostStyles.actions} px-6 py-4`}>
                          <div className="flex flex-wrap items-center justify-end gap-2">
                            {/* Draining is not offered as an action of its
                                own: Remove drains a busy host by itself. Only
                                a host already held back without a deployment
                                owning it — a pending removal, or a drain made
                                through the API — gets the way back. */}
                            {host.status === 'draining' && !host.drain_fence_active && (
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() =>
                                  void run(
                                    () => undrainAgentRunnerHost(host.host),
                                    `${name} is taking work again.`,
                                  )
                                }
                                className="ui-button ui-button-secondary min-h-9 px-3 py-1.5 text-xs [@media(pointer:coarse)]:min-h-11"
                              >
                                {host.removal_pending ? 'Cancel removal' : 'Resume new work'}
                              </button>
                            )}
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => handleRemoveClick(host)}
                              className="ui-button min-h-9 border border-status-error/20 px-3 py-1.5 text-xs text-status-error hover:bg-status-error/10 [@media(pointer:coarse)]:min-h-11"
                            >
                              {host.removal_pending ? 'Remove now' : 'Remove from pool'}
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
                            {settingDefinitions.length > 0 && (
                              <ServerSettingsPanel
                                name={name}
                                host={host}
                                definitions={settingDefinitions}
                                globalValues={hostSettings}
                                busy={busy}
                                onChange={(key, value) =>
                                  handleServerSetting(host.host, key, value)
                                }
                              />
                            )}
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
        {(scopedInactive.length > 0 || scopedHosts.some((host) => host.status === 'disabled')) && (
          <details className="mt-4 text-sm text-gray-500 dark:text-gray-400">
            <summary className="cursor-pointer">
              Unavailable servers and removed Host records
            </summary>
            <ul className="mt-2 space-y-2">
              {scopedInactive
                .filter((server) => !server.members.some((host) => host.status === 'disabled'))
                .map((server) => (
                  <li key={server.name}>
                    {server.name} — {server.host?.status ?? 'unavailable'}
                  </li>
                ))}
              {scopedHosts
                .filter((host) => host.status === 'disabled')
                .map((host) => (
                  <li key={host.host} className="flex flex-wrap items-center gap-3">
                    <span>{host.host} — disabled</span>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => handleForget(host.host)}
                      className="rounded-md border border-gray-200 px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-40 dark:border-white/10 dark:hover:bg-white/5"
                    >
                      Forget {host.host}
                    </button>
                  </li>
                ))}
            </ul>
          </details>
        )}
      </div>
    );
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
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Servers"
          description="Manage compute resources, server capacity, and sandbox placement."
        />
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

        <ComputeResources
          placementControl={
            <div className="mb-4 rounded-2xl border border-border bg-surface p-4 shadow-subtle sm:p-5">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <label htmlFor="server-placement" className="text-sm font-semibold text-foreground">
                  New sandbox location
                </label>
                <select
                  id="server-placement"
                  disabled={busy || pool === null}
                  value={activeHost ? (selectionUnavailable ? '__unavailable__' : activeHost) : ''}
                  onChange={(event) => void handlePin(event.target.value || null)}
                  className="ui-input min-h-11 w-full sm:w-auto sm:min-w-64"
                >
                  <option value="">Automatic — available servers</option>
                  {selectionUnavailable && (
                    <option value="__unavailable__" disabled>
                      Selected server unavailable
                    </option>
                  )}
                  {availableHosts.map((host) => {
                    const physicalName = servers.find((server) =>
                      server.members.some((member) => member.host === host.host),
                    )?.name;
                    const allocationId = computeInventory?.hosts.find(
                      (item) => item.host === host.host,
                    )?.allocation_id;
                    const allocation = computeInventory?.allocations.find(
                      (item) => item.id === allocationId,
                    );
                    const allocationHosts = allocation?.nodes.flatMap((node) => node.hosts) ?? [];
                    const displayName = allocation
                      ? allocationHosts.length > 1
                        ? `${allocation.name} / ${host.host}`
                        : allocation.name
                      : physicalName && physicalName !== host.host
                        ? `${physicalName} / ${host.host}`
                        : host.host;
                    return (
                      <option key={host.host} value={host.host}>
                        {displayName}
                      </option>
                    );
                  })}
                </select>
              </div>
              <p className="mt-3 text-sm leading-6 text-muted">
                All new sandboxes use this location. Existing sandboxes stay on their current
                server.
              </p>
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
            </div>
          }
          onHostsChanged={() => void load(true)}
          onInventory={setComputeInventory}
          activeHost={activeHost}
          serverGroups={pool === null ? undefined : servers}
          renderHostControls={renderHostControls}
          onHostControlsClosed={() => {
            setExpanded(null);
            setSourceChoice(null);
            setNotice(null);
          }}
          eligibleHosts={availableHosts.map((host) => host.host)}
          onUseHost={(host) => void handlePin(host)}
          settingBusy={busy}
          hostNotice={notice}
        />
      </section>
    </div>
  );
}

/** Two placeholder rows in the table's own shape, not a spinner over it. */
function TableSkeleton() {
  return (
    <div className="mt-4 overflow-hidden rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 shadow-sm">
      <table className="min-w-[1120px] w-full text-left text-sm">
        <thead className="border-b border-gray-100 dark:border-white/5 text-xs text-gray-500 dark:text-gray-400">
          <tr>
            <th className="px-6 py-3 font-medium">Server</th>
            <th className="px-3 py-3 font-medium">Status</th>
            <th className="px-3 py-3 font-medium whitespace-nowrap">Execution slots</th>
            <th className="px-3 py-3 font-medium">CPU</th>
            <th className="px-3 py-3 font-medium">Memory</th>
            <th className="px-3 py-3 font-medium">Storage</th>
            <th className="px-3 py-3 font-medium whitespace-nowrap">Sandboxes</th>
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
