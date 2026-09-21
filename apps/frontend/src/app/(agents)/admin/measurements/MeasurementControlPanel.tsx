'use client';

// Runtime control of measurement collection.
//
// The rest of this page reads what was collected. This card is the only place
// an admin can make the platform collect at all, so it is written to be slow to
// claim anything: a command is stored here, applied by a deployment elsewhere,
// and confirmed back by each participant. Every fact shown below is the
// server's, including the status after a successful write -- the response
// carries the control snapshot the command produced, and showing "Collecting"
// before that snapshot arrives would be this page's guess about a machine it
// cannot see.
//
// It also reads itself: a live run is re-read every five seconds, an idle
// deployment every thirty, and never while the tab is hidden. A failed
// background read keeps the last successful state on screen and says it is
// stale, because a card that empties itself on a network blip is a card that
// reports an outage nobody had.

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';

import { getAgentRunnerHosts, type AgentRunnerHost } from '@/lib/api/admin';
import {
  getProfilingControl,
  measurementOperationsFor,
  startProfilingRun,
  stopProfilingRun,
  type MeasurementOperationGroup,
  type ProfilingControlRun,
  type ProfilingControlSnapshot,
  type ProfilingControlStatus,
} from '@/lib/api/adminMeasurements';
import { getErrorMessage } from '@/lib/utils/errors';

import { accessDenied, BUTTON, CARD, dateTime, ErrorNotice, INPUT, number } from '../runs/shared';

/** A live run changes within seconds; an idle deployment does not. */
const LIVE_POLL_MS = 5000;
const IDLE_POLL_MS = 30000;
/** The countdown is local between polls, so it has to tick on its own. */
const TICK_MS = 1000;

/** The `<select>` value for "All hosts"; the API takes a null target for it. */
const ALL_HOSTS = '\u0000all-hosts';

const DURATION_MINUTES = [1, 5, 10, 30];
const DEFAULT_DURATION_MINUTES = 10;

const OPERATION_GROUPS: { group: MeasurementOperationGroup; label: string }[] = [
  { group: 'sandbox_startup', label: 'Sandbox startup' },
  { group: 'terminal_open', label: 'Terminal opening' },
  { group: 'file_access', label: 'File access' },
];
const ALL_GROUP_NAMES = OPERATION_GROUPS.map(({ group }) => group);

/**
 * The root operation names a run may name, in the words an operator uses.
 *
 * A name this map has never heard of is shown verbatim rather than dropped:
 * the run was asked for it, and hiding it would describe a different run.
 */
const OPERATION_WORDS: Record<string, string> = {
  'attempt.startup': 'Sandbox startup',
  'terminal.open': 'Terminal opening',
  'files.list': 'File access',
  'files.open': 'File access',
};

const LIVE_STATUSES = new Set<ProfilingControlStatus>([
  'starting',
  'collecting',
  'partially_active',
  'stopping',
]);
/** A run exists and can still be stopped from here. */
const STOPPABLE_STATUSES = new Set<ProfilingControlStatus>([
  'starting',
  'collecting',
  'partially_active',
]);
const STARTABLE_STATUSES = new Set<ProfilingControlStatus>(['off', 'stopped', 'expired']);
/**
 * States in which no command may be sent at all, whatever the run says. They
 * are not "off": off is a statement about collection, and these are statements
 * about whether this deployment can be commanded from here.
 */
const UNADMINISTRABLE_STATUSES = new Set<ProfilingControlStatus>([
  'unavailable',
  'unknown',
  'unmanaged',
]);

const STATUS_LABELS: Record<ProfilingControlStatus, string> = {
  off: 'Off',
  starting: 'Starting',
  collecting: 'Collecting',
  partially_active: 'Partly active',
  stopping: 'Stopping',
  stopped: 'Stopped',
  expired: 'Expired',
  unavailable: 'Unavailable',
  unknown: 'Unknown',
  unmanaged: 'Managed by deployment configuration',
};

const STATUS_DETAILS: Record<ProfilingControlStatus, string> = {
  off: 'Collect timings while you use sandboxes, terminals and files. Server-side timings only.',
  starting: 'The command is stored; the deployment has not confirmed it everywhere yet.',
  collecting: 'Server-side timings are being collected for the operations below.',
  partially_active: 'Collection is running, but not every participant has confirmed it yet.',
  stopping: 'The command is stored; participants are retiring the grants they were issued.',
  stopped: 'Collection stopped before its deadline. The stop reason is below.',
  expired: 'The experiment reached its deadline and closed itself. The reason is below.',
  unavailable:
    'This deployment is not configured for Admin control: it is not running in Admin ' +
    'measurement mode. That is not the same as off, and no Start is offered here.',
  unknown:
    'The control plane cannot say what state measurement collection is in. That is not the ' +
    'same as off, and nothing is started or stopped from here.',
  unmanaged:
    "Measurement collection is set by this deployment's own configuration, not by Admin " +
    'commands. That is not the same as off, and no Start is offered here.',
};

function statusLabel(status: ProfilingControlStatus): string {
  return STATUS_LABELS[status] ?? status;
}

function unadministrableDetail(control: ProfilingControlSnapshot): string {
  if (control.status === 'unknown' || control.status === 'unmanaged') {
    return STATUS_DETAILS[control.status];
  }
  if (!control.configured) {
    return (
      'This deployment is not configured for Admin control: it has never been commanded, so ' +
      'there is no control state to start or stop from here. That is not the same as off.'
    );
  }
  return STATUS_DETAILS.unavailable;
}

function durationLabel(minutes: number): string {
  return minutes === 1 ? '1 minute' : `${minutes} minutes`;
}

function operationWords(operations: string[]): string {
  const words: string[] = [];
  for (const operation of operations) {
    const word = OPERATION_WORDS[operation] ?? operation;
    if (!words.includes(word)) words.push(word);
  }
  return words.length > 0 ? words.join(', ') : 'None selected';
}

/**
 * A monotonic reading, so a clock adjustment cannot make a countdown move
 * backwards. Only differences are ever shown.
 */
function monotonicNow(): number {
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

function mmss(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

/**
 * A request id the server can deduplicate on.
 *
 * `crypto.randomUUID` is the real thing; the fallback exists because it is
 * absent outside a secure context, and a command that cannot be retried
 * idempotently is worse than one carrying a weaker id.
 */
function newRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    try {
      return crypto.randomUUID();
    } catch {
      // Fall through: some browsers expose the name but refuse outside TLS.
    }
  }
  return `measurement-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

interface PanelSnapshot {
  control: ProfilingControlSnapshot;
  /** The monotonic instant the local countdown counts down to, or null. */
  expiresAtMs: number | null;
}

export function MeasurementControlPanel({
  onDenied,
  refreshKey,
}: {
  onDenied: () => void;
  refreshKey: number;
}) {
  const [snapshot, setSnapshot] = useState<PanelSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState<string | null>(null);
  /** A failed background poll: the last good snapshot stays, flagged. */
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [write, setWrite] = useState<'start' | 'stop' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [groups, setGroups] = useState<MeasurementOperationGroup[]>(ALL_GROUP_NAMES);
  const [host, setHost] = useState('');
  const [minutes, setMinutes] = useState(DEFAULT_DURATION_MINUTES);

  const [hosts, setHosts] = useState<AgentRunnerHost[] | null>(null);
  const [hostsError, setHostsError] = useState<string | null>(null);

  const [nowMs, setNowMs] = useState(monotonicNow);

  const mounted = useRef(true);
  const readRequest = useRef<AbortController | null>(null);
  const writeRequest = useRef<AbortController | null>(null);
  /** The newest read, so a slow one cannot land on a newer answer. */
  const readSeq = useRef(0);
  /** Bumped whenever a snapshot is applied, so a write beats an earlier poll. */
  const appliedSeq = useRef(0);
  const hasData = useRef(false);
  /** The start command a retry would repeat, and the id that makes it one command. */
  const startAttempt = useRef<{ fingerprint: string; id: string } | null>(null);
  /** The stop command a retry would repeat. */
  const stopAttempt = useRef<{ runId: string; id: string } | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      readRequest.current?.abort();
      writeRequest.current?.abort();
    };
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setNowMs(monotonicNow()), TICK_MS);
    return () => clearInterval(timer);
  }, []);

  const applyControl = useCallback((next: ProfilingControlSnapshot) => {
    hasData.current = true;
    // The countdown is pinned to the reading the same response carried, and
    // the clock is moved with it: otherwise a poll that lands 300 ms after the
    // last tick would render 601 seconds for a 600-second deadline.
    const applied = monotonicNow();
    setSnapshot({
      control: next,
      expiresAtMs:
        next.run === null ? null : applied + Math.max(0, next.run.seconds_remaining) * 1000,
    });
    setNowMs(applied);
    setRefreshFailed(false);
  }, []);

  const clearAll = useCallback(() => {
    readRequest.current?.abort();
    writeRequest.current?.abort();
    readSeq.current += 1;
    hasData.current = false;
    setSnapshot(null);
    setReadError(null);
    setRefreshFailed(false);
    setWrite(null);
    setFormOpen(false);
    setActionError(null);
    setHostsError(null);
    onDenied();
  }, [onDenied]);

  const load = useCallback(
    async (background: boolean) => {
      readRequest.current?.abort();
      const controller = new AbortController();
      readRequest.current = controller;
      if (!background) setLoading(true);
      setReadError(null);
      const seq = (readSeq.current += 1);
      // A poll issued before a write must not land on top of its answer: the
      // write's snapshot is a fresh read of the same control row and is always
      // the newer fact.
      const applied = appliedSeq.current;
      try {
        const next = await getProfilingControl(controller.signal);
        if (!mounted.current || controller.signal.aborted) return;
        if (seq !== readSeq.current || applied !== appliedSeq.current) return;
        applyControl(next);
        appliedSeq.current += 1;
      } catch (e) {
        if (!mounted.current || controller.signal.aborted) return;
        if (accessDenied(e)) {
          // A revoked session must not keep showing a control state, and must
          // never be read as "off": the page renders AdminRequired instead.
          clearAll();
          return;
        }
        // A failed background poll keeps the last good state on screen; the
        // numbers going stale is a smaller lie than claiming there is none.
        if (background && hasData.current) setRefreshFailed(true);
        else setReadError(getErrorMessage(e));
      } finally {
        if (mounted.current && seq === readSeq.current && !background) setLoading(false);
      }
    },
    [applyControl, clearAll],
  );

  const live = snapshot !== null && LIVE_STATUSES.has(snapshot.control.status);

  useEffect(() => {
    void load(hasData.current);
    const timer = setInterval(
      () => {
        if (document.hidden) return;
        void load(true);
      },
      live ? LIVE_POLL_MS : IDLE_POLL_MS,
    );
    return () => clearInterval(timer);
  }, [load, live, refreshKey]);

  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden) return;
      void load(true);
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [load]);

  // The host menu is read when the form is opened, not with every poll: a
  // machine that joins the pool mid-run does not change which hosts this run
  // could have been aimed at.
  useEffect(() => {
    if (!formOpen) return;
    const controller = new AbortController();
    setHostsError(null);
    getAgentRunnerHosts(controller.signal)
      .then((pool) => {
        if (controller.signal.aborted) return;
        setHosts(pool.hosts ?? []);
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        if (accessDenied(e)) {
          clearAll();
          return;
        }
        // The menu is short by one read, not wrong: All hosts is still offered
        // and no host name is invented to fill the gap.
        setHostsError(getErrorMessage(e));
      });
    return () => controller.abort();
  }, [formOpen, clearAll]);

  const control = snapshot?.control ?? null;
  const status = control?.status ?? null;
  const administrable =
    control !== null && control.configured && !UNADMINISTRABLE_STATUSES.has(control.status);
  const detail =
    control === null
      ? null
      : administrable
        ? STATUS_DETAILS[control.status]
        : unadministrableDetail(control);
  const canStart = administrable && status !== null && STARTABLE_STATUSES.has(status);
  const canStop =
    administrable &&
    control !== null &&
    control.run !== null &&
    status !== null &&
    STOPPABLE_STATUSES.has(status);
  const activeRun =
    administrable && control !== null && status !== null && LIVE_STATUSES.has(status)
      ? control.run
      : null;
  const stoppedRun =
    administrable && control !== null && (status === 'stopped' || status === 'expired')
      ? control.run
      : null;
  const secondsLeft =
    snapshot?.expiresAtMs == null
      ? null
      : Math.max(0, Math.ceil((snapshot.expiresAtMs - nowMs) / 1000));
  const busy = write !== null;

  const toggleGroup = (group: MeasurementOperationGroup) => {
    setGroups((current) =>
      ALL_GROUP_NAMES.filter((name) =>
        name === group ? !current.includes(name) : current.includes(name),
      ),
    );
  };

  const openForm = () => {
    setActionError(null);
    setFormOpen(true);
  };

  const closeForm = () => {
    setActionError(null);
    setHostsError(null);
    setFormOpen(false);
  };

  const submitStart = async (event: FormEvent) => {
    event.preventDefault();
    if (control === null || busy) return;
    if (groups.length === 0) {
      setActionError('Choose at least one thing to measure. Collection needs a selection.');
      return;
    }
    const operations = measurementOperationsFor(groups, control);
    if (operations.length === 0) {
      setActionError(
        'This deployment offers no operation for the groups you chose, so there is nothing to ' +
          'collect. Choose another group.',
      );
      return;
    }
    if (host === '') {
      setActionError('Choose a host, or All hosts. Nothing is aimed at a host you did not pick.');
      return;
    }
    const targetHost = host === ALL_HOSTS ? null : host;
    // One id per command, not per click: a retry of the same submission is the
    // same command, and a changed submission is a new one.
    const fingerprint = JSON.stringify({
      revision: control.revision,
      operations,
      host: targetHost,
      duration: minutes,
    });
    const requestId =
      startAttempt.current?.fingerprint === fingerprint ? startAttempt.current.id : newRequestId();
    startAttempt.current = { fingerprint, id: requestId };

    const controller = new AbortController();
    writeRequest.current = controller;
    setWrite('start');
    setActionError(null);
    try {
      const response = await startProfilingRun(
        {
          request_id: requestId,
          expected_revision: control.revision,
          operations,
          target: { host: targetHost },
          duration_seconds: minutes * 60,
        },
        controller.signal,
      );
      if (!mounted.current || controller.signal.aborted) return;
      // The server's snapshot is what is drawn; nothing here is assumed.
      readSeq.current += 1;
      appliedSeq.current += 1;
      applyControl(response.control);
      startAttempt.current = null;
      setFormOpen(false);
      setHostsError(null);
    } catch (e) {
      if (!mounted.current || controller.signal.aborted) return;
      if (accessDenied(e)) {
        clearAll();
        return;
      }
      // The form stays open with the values that produced the failure: the
      // retry is the same submission, and the next attempt reuses its id.
      setActionError(getErrorMessage(e));
    } finally {
      if (mounted.current) setWrite(null);
    }
  };

  const submitStop = async () => {
    if (control === null || control.run === null || busy) return;
    const runId = control.run.run_id;
    const requestId =
      stopAttempt.current?.runId === runId ? stopAttempt.current.id : newRequestId();
    stopAttempt.current = { runId, id: requestId };

    const controller = new AbortController();
    writeRequest.current = controller;
    setWrite('stop');
    setActionError(null);
    try {
      const response = await stopProfilingRun(runId, { request_id: requestId }, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      readSeq.current += 1;
      appliedSeq.current += 1;
      applyControl(response.control);
      stopAttempt.current = null;
    } catch (e) {
      if (!mounted.current || controller.signal.aborted) return;
      if (accessDenied(e)) {
        clearAll();
        return;
      }
      setActionError(getErrorMessage(e));
    } finally {
      if (mounted.current) setWrite(null);
    }
  };

  return (
    <section className={`${CARD} mt-6 p-5`} aria-labelledby="measurement-control-heading">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h3
            id="measurement-control-heading"
            className="text-sm font-semibold text-gray-900 dark:text-gray-50"
          >
            Measurement collection
          </h3>
          {status === null ? (
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              {loading ? 'Reading the control state…' : 'No control state has been read.'}
            </p>
          ) : (
            <>
              <p className="mt-1 text-base font-medium text-gray-900 dark:text-gray-50">
                {statusLabel(status)}
              </p>
              <p className="mt-1 max-w-3xl text-sm text-gray-500 dark:text-gray-400">{detail}</p>
            </>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canStart && !formOpen && (
            <button className={BUTTON} type="button" disabled={busy} onClick={openForm}>
              Start measurement
            </button>
          )}
          {canStop && (
            <button className={BUTTON} type="button" disabled={busy} onClick={submitStop}>
              Stop measurement
            </button>
          )}
        </div>
      </div>

      {readError !== null && <ErrorNotice>{readError}</ErrorNotice>}

      {refreshFailed && (
        <p className="mt-3 text-xs text-amber-700 dark:text-amber-400">
          The last refresh failed; showing the last successful read.
        </p>
      )}

      {control !== null && control.gaps.length > 0 && (
        <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-amber-800 dark:text-amber-300">
          {control.gaps.map((gap) => (
            <li key={`${gap.kind}\u0000${gap.detail}`}>
              <span className="font-medium">{gap.kind}</span>
              <span className="block">{gap.detail}</span>
            </li>
          ))}
        </ul>
      )}

      {control !== null && activeRun !== null && (
        <RunFacts control={control} run={activeRun} secondsLeft={secondsLeft} />
      )}

      {control !== null && stoppedRun !== null && (
        <div className="mt-3 space-y-1 text-sm text-gray-700 dark:text-gray-300">
          <p>
            {stoppedRun.stop_reason
              ? `Stop reason: ${stoppedRun.stop_reason}`
              : 'The control plane recorded no stop reason for this run.'}
          </p>
          {stoppedRun.stopped_at !== null && (
            <p>{`Stopped at: ${dateTime(stoppedRun.stopped_at)}`}</p>
          )}
        </div>
      )}

      {actionError !== null && <ErrorNotice>{actionError}</ErrorNotice>}

      {formOpen && control !== null && (
        <form
          className="mt-4 border-t border-gray-100 pt-4 dark:border-white/5"
          onSubmit={submitStart}
        >
          <p className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
            What to measure
          </p>
          <div className="mt-2 flex flex-wrap gap-x-6 gap-y-2">
            {OPERATION_GROUPS.map(({ group, label }) => (
              <label
                key={group}
                className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300"
              >
                <input
                  type="checkbox"
                  className="h-4 w-4 rounded border-gray-300 text-crimson focus:ring-crimson dark:border-white/20 dark:bg-gray-900"
                  checked={groups.includes(group)}
                  disabled={busy}
                  onChange={() => toggleGroup(group)}
                />
                {label}
              </label>
            ))}
          </div>

          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <div>
              <label
                className="block text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
                htmlFor="measurement-target-host"
              >
                Host
              </label>
              <select
                id="measurement-target-host"
                className={`${INPUT} mt-1`}
                value={host}
                disabled={busy}
                onChange={(event) => setHost(event.target.value)}
              >
                <option value="">Choose a host…</option>
                <option value={ALL_HOSTS}>All hosts</option>
                {(hosts ?? []).map((option) => (
                  <option key={option.host} value={option.host}>
                    {option.host}
                  </option>
                ))}
              </select>
              {hostsError !== null && (
                <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">
                  {`${hostsError} The host list could not be read; All hosts is still offered and no host name was invented.`}
                </p>
              )}
            </div>

            <div>
              <label
                className="block text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
                htmlFor="measurement-duration"
              >
                Duration
              </label>
              <select
                id="measurement-duration"
                className={`${INPUT} mt-1`}
                value={String(minutes)}
                disabled={busy}
                onChange={(event) => setMinutes(Number(event.target.value))}
              >
                {DURATION_MINUTES.map((value) => (
                  <option key={value} value={String(value)}>
                    {durationLabel(value)}
                  </option>
                ))}
              </select>
              {Number.isFinite(control.bounds?.min_duration_seconds) && (
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  {`The server accepts ${control.bounds.min_duration_seconds}–${control.bounds.max_duration_seconds} seconds.`}
                </p>
              )}
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button className={BUTTON} type="submit" disabled={busy}>
              Start measurement
            </button>
            <button className={BUTTON} type="button" disabled={busy} onClick={closeForm}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

/** The facts of a run that is still open, and what a stored sample is. */
function RunFacts({
  control,
  run,
  secondsLeft,
}: {
  control: ProfilingControlSnapshot;
  run: ProfilingControlRun;
  secondsLeft: number | null;
}) {
  return (
    <>
      <ul className="mt-3 space-y-1 text-sm text-gray-700 dark:text-gray-300">
        <li>{`Operations: ${operationWords(run.operations)}`}</li>
        <li>{`Host: ${run.target?.host ? run.target.host : 'All hosts'}`}</li>
        <li>{`Time left: ${secondsLeft === null ? 'Unavailable' : mmss(secondsLeft)}`}</li>
        <li>{`Stored samples: ${number(control.stored_samples)}`}</li>
      </ul>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        A stored sample is one server-side record, and one terminal interaction can produce several.
      </p>
      {control.budget_note !== '' && (
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{control.budget_note}</p>
      )}
    </>
  );
}
