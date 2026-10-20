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

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import type {
  AgentHostCapacityHistory,
  AgentRunnerHost,
  AgentRunnerHostPool,
  CapacityRange,
  HostSettingDefinition,
  HostSettingValues,
} from '@/lib/api/admin';
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
import { HostGroupRows } from './HostGroupRows';
import { ServerSettingsPanel } from './HostSettings';
import hostStyles from './host-controls.module.css';
import {
  type HostGroupCapacity,
  formatUtcTimestamp,
  groupCapacityFacts,
  liveAgeSeconds,
  monotonicNow,
} from './capacity';
import { curveSource, curveSourceHosts, formatAge, serverInventory } from './format';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { AdminRequired } from '../runs/shared';

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
                  const allocationId = computeInventory?.hosts.find(
                    (item) => item.host === host.host,
                  )?.allocation_id;
                  const autoscalingManaged =
                    computeInventory?.allocations
                      .find((allocation) => allocation.id === allocationId)
                      ?.blocked_reasons.some((reason) => reason.code === 'autoscaling_managed') ??
                    false;
                  return (
                    <HostGroupRows
                      key={name}
                      name={name}
                      host={host}
                      facts={facts}
                      open={open}
                      panelId={panelId}
                      autoscalingManaged={autoscalingManaged}
                      busy={busy}
                      onToggle={() => toggleServer(name, facts)}
                      onSlots={(slots) => void handleSlots(host.host, slots)}
                      onResume={() =>
                        void run(
                          () => undrainAgentRunnerHost(host.host),
                          `${name} is taking work again.`,
                        )
                      }
                      onRemove={() => handleRemoveClick(host)}
                      panel={
                        open && openServer && openFacts ? (
                          <>
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
                          </>
                        ) : null
                      }
                    />
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
    return <AdminRequired>Your access to the runner host pool was revoked.</AdminRequired>;
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
