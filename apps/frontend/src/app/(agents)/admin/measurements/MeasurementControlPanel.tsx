'use client';

// Runtime control of measurement collection.
//
// The rest of this page reads what was collected. This card is the only place
// an admin can make the platform collect at all, so it is written to be slow to
// claim anything. What it shows is the deployment's *configuration*: the
// experiment currently in force, until when, and what that configuration has
// stored. It deliberately says nothing about which processes have applied it --
// the configuration reaches them through a background sync, and the server's
// own `sync_note` is what the card repeats instead of pretending to have
// measured the lag.
//
// It reads as one line -- "Measurement on · what · where · since · samples" --
// and one button, whose label follows the state, is the whole surface:
//
//   * off  -- "Turn on" opens the inline form;
//   * on   -- "Turn off" stops the run immediately, with no confirmation,
//             because the command is reversible in one click;
//   * a state this deployment cannot be commanded in (unavailable, unmanaged)
//             offers no button at all and explains why instead.
//
// Every fact shown after a write is the server's: the response carries the
// control snapshot the command produced, and showing "Enabled" before that
// snapshot arrives would be this page's guess about a machine it cannot see.
//
// It also reads itself: a live run is re-read every five seconds, an idle
// deployment every thirty, and never while the tab is hidden. A failed
// background read keeps the last successful state on screen and says it is
// stale, because a card that empties itself on a network blip is a card that
// reports an outage nobody had.

import {
  Fragment,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react';

import { getAgentRunnerHosts, type AgentRunnerHost } from '@/lib/api/admin';
import {
  getProfilingControl,
  measurementOperationsFor,
  startProfilingRun,
  stopProfilingRun,
  type MeasurementOperationGroup,
  type ProfilingControlRun,
  type ProfilingControlSnapshot,
} from '@/lib/api/adminMeasurements';
import { getErrorMessage } from '@/lib/utils/errors';

import { accessDenied, BUTTON, CARD, dateTime, ErrorNotice, INPUT, number } from '../runs/shared';
import { shortTime } from './present';

/** A live experiment changes within seconds; an idle deployment does not. */
const LIVE_POLL_MS = 5000;
const IDLE_POLL_MS = 30000;
/** The countdown is local between polls, so it has to tick on its own. */
const TICK_MS = 1000;

/** The `<select>` value for "All hosts"; the API takes a null target for it. */
const ALL_HOSTS = '\u0000all-hosts';

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

/** What one stored sample is, said once, beside the count it is a count of. */
const SAMPLE_NOTE =
  'A stored sample is one server-side record, and one terminal interaction can produce several.';

/**
 * The one-line status, which is a statement about configuration.
 *
 * It is not a consensus and must not be read as one: collection is "on" the
 * moment the deployment is configured for it, whether or not every process
 * has caught up -- which is why it says "Measurement on" rather than
 * "Measuring", which would claim samples are arriving.
 */
function statusLine(status: string): string {
  if (status === 'off') return 'Measurement off';
  if (status === 'on') return 'Measurement on';
  if (status === 'unmanaged') return 'Measurement set by deployment configuration';
  return 'Measurement unavailable';
}

/**
 * Why a deployment offers no switch, for the two states that have none.
 *
 * Neither is "off": off is a statement about collection, and these are
 * statements about whether this page may command the deployment at all.
 */
function noSwitchReason(status: string): string | null {
  if (status === 'unmanaged') {
    return (
      "This deployment's own configuration decides what is measured, so there is no switch " +
      'here. It may still be collecting.'
    );
  }
  if (status === 'unavailable') {
    return 'This deployment is not set up for Admin control, so there is nothing to switch here.';
  }
  return null;
}

/** A stop reason in words, or nothing for the ordinary one. */
function stopWords(reason: string | null): string | null {
  if (!reason || reason === 'stopped' || reason === 'stopped_by_admin') return null;
  if (reason === 'expired') return 'expired';
  return reason;
}

/** The server's sync note as a sentence. */
function sentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return '';
  const capital = `${trimmed.charAt(0).toUpperCase()}${trimmed.slice(1)}`;
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}

function operationWords(operations: string[]): string {
  const words: string[] = [];
  for (const operation of operations) {
    const word = OPERATION_WORDS[operation] ?? operation;
    if (!words.includes(word)) words.push(word);
  }
  return words.length > 0 ? words.join(', ') : 'None selected';
}

function hostWords(run: ProfilingControlRun): string {
  return run.target?.host ? run.target.host : 'All hosts';
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
  /** Which write is in flight, so the button can show its pending verb. */
  const [write, setWrite] = useState<'enable' | 'disable' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  /** A write succeeded here, so the sync lag is worth saying out loud. */
  const [changed, setChanged] = useState(false);

  const [formOpen, setFormOpen] = useState(false);
  const [groups, setGroups] = useState<MeasurementOperationGroup[]>(ALL_GROUP_NAMES);
  const [host, setHost] = useState('');

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
  /** The enable command a retry would repeat, and the id that makes it one command. */
  const enableAttempt = useRef<{ fingerprint: string; id: string } | null>(null);
  /** The disable command a retry would repeat. */
  const disableAttempt = useRef<{ runId: string; id: string } | null>(null);

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
        next.run?.expires_at == null
          ? null
          : applied + Math.max(0, next.run.seconds_remaining) * 1000,
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
    setChanged(false);
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

  /** Collecting means the configuration says so *and* the run is in force. */
  const live = snapshot !== null && snapshot.control.status === 'on';

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
        setHosts(
          (pool.hosts ?? []).filter(
            (item) => item.enrolled && ['online', 'draining'].includes(item.status),
          ),
        );
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
  const busy = write !== null;
  /**
   * Whether this deployment can be commanded from here at all.
   *
   * `unavailable` and `unmanaged` are not "off": off is a statement about
   * collection, and these are statements about whether this page may command
   * the deployment. A deployment with no control row is neither.
   */
  const commandable =
    control !== null &&
    control.configured === true &&
    (control.status === 'off' || control.status === 'on');
  const collecting =
    control !== null &&
    control.configured === true &&
    control.status === 'on' &&
    control.run_active === true &&
    control.run !== null;
  const activeRun = collecting ? (control?.run ?? null) : null;
  const stoppedRun = control !== null && control.status === 'off' ? control.last_stopped_run : null;
  const secondsLeft =
    snapshot?.expiresAtMs == null
      ? null
      : Math.max(0, Math.ceil((snapshot.expiresAtMs - nowMs) / 1000));

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

  const submitEnable = async (event: FormEvent) => {
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
    // Nothing is preselected, so an empty value is a decision the admin has not
    // made yet -- not an implicit "All hosts".
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
    });
    const requestId =
      enableAttempt.current?.fingerprint === fingerprint
        ? enableAttempt.current.id
        : newRequestId();
    enableAttempt.current = { fingerprint, id: requestId };

    const controller = new AbortController();
    writeRequest.current = controller;
    setWrite('enable');
    setActionError(null);
    try {
      const response = await startProfilingRun(
        {
          request_id: requestId,
          expected_revision: control.revision,
          operations,
          target: { host: targetHost },
          duration_seconds: null,
        },
        controller.signal,
      );
      if (!mounted.current || controller.signal.aborted) return;
      // The server's snapshot is what is drawn; nothing here is assumed.
      readSeq.current += 1;
      appliedSeq.current += 1;
      applyControl(response.control);
      setChanged(true);
      enableAttempt.current = null;
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

  const submitDisable = async () => {
    const run = collecting ? (control?.run ?? null) : null;
    if (run === null || busy) return;
    const runId = run.run_id;
    const requestId =
      disableAttempt.current?.runId === runId ? disableAttempt.current.id : newRequestId();
    disableAttempt.current = { runId, id: requestId };

    const controller = new AbortController();
    writeRequest.current = controller;
    setWrite('disable');
    setActionError(null);
    try {
      const response = await stopProfilingRun(runId, { request_id: requestId }, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      readSeq.current += 1;
      appliedSeq.current += 1;
      applyControl(response.control);
      setChanged(true);
      disableAttempt.current = null;
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

  const buttonLabel = busy
    ? write === 'disable'
      ? 'Turning off…'
      : 'Turning on…'
    : collecting
      ? 'Turn off'
      : 'Turn on';
  const reason = control === null ? null : noSwitchReason(control.status);
  const dot = collecting
    ? 'bg-emerald-500'
    : control?.status === 'off'
      ? 'bg-gray-300 dark:bg-gray-600'
      : 'bg-amber-500';

  return (
    <section className={`${CARD} mt-6 px-5 py-4`} aria-labelledby="measurement-control-heading">
      <h3 id="measurement-control-heading" className="sr-only">
        Measurement collection
      </h3>
      <div className="flex flex-wrap items-center justify-between gap-3">
        {control === null ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {loading
              ? 'Reading whether measurement is on…'
              : 'Could not read whether measurement is on.'}
          </p>
        ) : (
          <p className="min-w-0 text-sm text-gray-500 dark:text-gray-400">
            <span
              aria-hidden
              className={`mr-2 inline-block h-2 w-2 rounded-full align-middle ${dot}`}
            />
            <span
              className="font-medium text-gray-900 dark:text-gray-100"
              title={control.sync_note ? sentence(control.sync_note) : undefined}
            >
              {statusLine(control.status)}
            </span>
            {activeRun !== null && (
              <RunFacts control={control} run={activeRun} secondsLeft={secondsLeft} />
            )}
            {stoppedRun !== null && <StoppedRun run={stoppedRun} />}
          </p>
        )}
        {control !== null && commandable && !formOpen && (
          <button
            className={BUTTON}
            type="button"
            disabled={busy}
            onClick={collecting ? submitDisable : openForm}
          >
            {buttonLabel}
          </button>
        )}
      </div>

      {readError !== null && <ErrorNotice>{readError}</ErrorNotice>}

      {refreshFailed && (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
          Couldn’t refresh; showing the last successful read.
        </p>
      )}

      {reason !== null && <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">{reason}</p>}

      {activeRun !== null && control?.stored_samples === 0 && (
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
          No samples yet. Use a sandbox, open a terminal or browse files, and they will appear
          below.
        </p>
      )}

      {changed && control !== null && control.sync_note !== '' && (
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
          {sentence(control.sync_note)}
        </p>
      )}

      {actionError !== null && <ErrorNotice>{actionError}</ErrorNotice>}

      {formOpen && control !== null && (
        <form
          className="mt-4 flex flex-wrap items-end gap-x-8 gap-y-4 border-t border-gray-100 pt-4 dark:border-white/5"
          onSubmit={submitEnable}
        >
          <fieldset>
            <legend className="text-xs font-medium text-gray-500 dark:text-gray-400">
              Measure
            </legend>
            <div className="mt-2.5 flex flex-wrap gap-x-5 gap-y-2">
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
          </fieldset>

          <div className="w-60">
            <label
              className="block text-xs font-medium text-gray-500 dark:text-gray-400"
              htmlFor="measurement-target-host"
            >
              Host
            </label>
            <select
              id="measurement-target-host"
              className={`${INPUT} mt-1 py-1.5`}
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
          </div>

          <div className="flex items-center gap-2">
            <button className={BUTTON} type="submit" disabled={busy}>
              {busy ? 'Turning on…' : 'Turn on'}
            </button>
            <button className={BUTTON} type="button" disabled={busy} onClick={closeForm}>
              Cancel
            </button>
          </div>

          {hostsError !== null && (
            <p className="basis-full text-xs text-amber-700 dark:text-amber-400">
              {`Couldn’t read the host list (${hostsError}). All hosts is still offered.`}
            </p>
          )}
        </form>
      )}
    </section>
  );
}

/**
 * Facts that continue the status line, each after a separator.
 *
 * The separator is glued to the fact before it, so a narrow card wraps
 * between facts rather than leaving a lone "·" on a line of its own.
 */
function Facts({ items }: { items: ReactNode[] }) {
  return (
    <>
      {items.map((item, index) => (
        <Fragment key={index}>
          {'\u00a0· '}
          {item}
        </Fragment>
      ))}
    </>
  );
}

/** What a run in force measures, where, since when, and what it has stored so far. */
function RunFacts({
  control,
  run,
  secondsLeft,
}: {
  control: ProfilingControlSnapshot;
  run: ProfilingControlRun;
  secondsLeft: number | null;
}) {
  const samples = control.stored_samples;
  const items: ReactNode[] = [
    <span key="operations">{operationWords(run.operations)}</span>,
    <span key="host">{hostWords(run)}</span>,
  ];
  if (run.started_at) {
    items.push(
      <span key="since" title={dateTime(run.started_at)}>
        since {shortTime(run.started_at)}
      </span>,
    );
  }
  items.push(
    <span key="samples" className="cursor-help" title={SAMPLE_NOTE}>
      {number(samples)} sample{samples === 1 ? '' : 's'}
    </span>,
  );
  if (run.expires_at !== null) {
    items.push(
      <span key="expires" title={`Stops at ${dateTime(run.expires_at)}`}>
        {secondsLeft === null ? 'stops at a set time' : `stops in ${mmss(secondsLeft)}`}
      </span>,
    );
  }
  return <Facts items={items} />;
}

/** The run the most recent stop ended, as context for "off". */
function StoppedRun({ run }: { run: ProfilingControlRun }) {
  const why = stopWords(run.stop_reason);
  return (
    <Facts
      items={[
        <span key="stopped" title={run.run_id}>
          {run.stopped_at ? `last run stopped ${shortTime(run.stopped_at)}` : 'last run stopped'}
          {why ? ` (${why})` : ''}
        </span>,
      ]}
    />
  );
}
