'use client';

// Admin → Audit log: every change made from the admin pages, newest first.
//
// One sentence per change, who made it and when; the dotted action, the
// caller's address and the raw details open under it for anyone checking the
// record itself. Polling reads never reach this page (the route leaves them
// out), so what is here is what someone changed, or a user's content someone
// viewed.

import { useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type {
  AdminAuditCategory,
  AdminAuditLogEntry,
  HostSettingDefinition,
} from '@/lib/api/admin';
import { getAgentRunnerHosts, listAdminAuditLog } from '@/lib/api/admin';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { dateTime } from '../runs/shared';
import { auditOutcome, describeActor, describeAuditEntry, describeWhen } from './describe';

const FILTERS: { key: AdminAuditCategory | null; label: string }[] = [
  { key: null, label: 'All' },
  { key: 'settings', label: 'Settings' },
  { key: 'servers', label: 'Servers' },
  { key: 'measurements', label: 'Measurements' },
  { key: 'content', label: 'Content views' },
  { key: 'database', label: 'Database' },
];

export default function AdminAuditLogPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
    return <AccessDenied />;
  }
  return <AuditLogView key={state.user?.id ?? 'admin'} userId={state.user?.id ?? null} />;
}

function AccessDenied() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
      <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
        Admin access required
      </h1>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        The audit log is visible to admins only.
      </p>
    </div>
  );
}

function AuditLogView({ userId }: { userId: string | null }) {
  const [category, setCategory] = useState<AdminAuditCategory | null>(null);
  const [entries, setEntries] = useState<AdminAuditLogEntry[] | null>(null);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  // Labels and units for settings changes; without them a change still reads,
  // with the setting's key in place of its name.
  const [definitions, setDefinitions] = useState<HostSettingDefinition[]>([]);
  const request = useRef<AbortController | null>(null);

  const fail = useCallback((cause: unknown) => {
    if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
      setDenied(true);
    } else {
      setError(getErrorMessage(cause));
    }
  }, []);

  const load = useCallback(
    async (before: number | null) => {
      request.current?.abort();
      const controller = new AbortController();
      request.current = controller;
      setError(null);
      if (before === null) {
        // A cursor belongs to the list it came from; a new first page, or a
        // failed one, must not offer the old list's next page.
        setEntries(null);
        setNextBefore(null);
        setOpen(null);
        setLoadingMore(false);
      } else {
        setLoadingMore(true);
      }
      try {
        const page = await listAdminAuditLog({ category, before }, controller.signal);
        if (controller.signal.aborted) return;
        setEntries((current) =>
          before === null ? page.entries : [...(current ?? []), ...page.entries],
        );
        setNextBefore(page.next_before);
      } catch (cause) {
        if (controller.signal.aborted) return;
        fail(cause);
        if (before === null) setEntries([]);
      } finally {
        if (!controller.signal.aborted) setLoadingMore(false);
      }
    },
    [category, fail],
  );

  useEffect(() => {
    void load(null);
    return () => request.current?.abort();
  }, [load]);

  useEffect(() => {
    const controller = new AbortController();
    getAgentRunnerHosts(controller.signal)
      .then((pool) => setDefinitions(pool.host_setting_definitions ?? []))
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  if (denied) return <AccessDenied />;

  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Audit log"
          description="Review administrator actions and configuration changes."
        />
        <AdminNavigation current="audit" />

        <div className="mt-8 max-w-4xl">
          <div
            role="group"
            aria-label="Show"
            className="inline-flex flex-wrap gap-1 rounded-xl border border-border bg-surface-muted p-1"
          >
            {FILTERS.map((item) => (
              <button
                key={item.label}
                type="button"
                aria-pressed={item.key === category}
                onClick={() => setCategory(item.key)}
                className={`ui-focus-ring min-h-11 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                  item.key === category
                    ? 'bg-surface text-foreground shadow-subtle'
                    : 'text-muted hover:bg-surface-hover hover:text-foreground'
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>

          {error && (
            <div
              role="alert"
              className="mt-6 flex items-center justify-between gap-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-400"
            >
              <span>{error}</span>
              <button
                type="button"
                onClick={() => void load(null)}
                className="shrink-0 font-medium underline"
              >
                Retry
              </button>
            </div>
          )}

          {entries === null ? (
            <p role="status" className="mt-8 text-sm text-gray-500 dark:text-gray-400">
              Loading…
            </p>
          ) : entries.length === 0 ? (
            !error && (
              <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
                Nothing recorded here yet.
              </p>
            )
          ) : (
            <ul className="ui-card mt-6 divide-y divide-border overflow-hidden rounded-2xl">
              {entries.map((entry) => (
                <AuditRow
                  key={entry.id}
                  entry={entry}
                  userId={userId}
                  definitions={definitions}
                  open={open === entry.id}
                  onToggle={() => setOpen(open === entry.id ? null : entry.id)}
                />
              ))}
            </ul>
          )}

          {entries !== null && nextBefore !== null && (
            <button
              type="button"
              disabled={loadingMore}
              onClick={() => void load(nextBefore)}
              className="ui-button ui-button-secondary mt-4 min-h-11"
            >
              {loadingMore ? 'Loading…' : 'Show older'}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}

function AuditRow({
  entry,
  userId,
  definitions,
  open,
  onToggle,
}: {
  entry: AdminAuditLogEntry;
  userId: string | null;
  definitions: HostSettingDefinition[];
  open: boolean;
  onToggle: () => void;
}) {
  const actor = describeActor(entry.actor_id, userId);
  const outcome = auditOutcome(entry);
  const exact = dateTime(entry.created_at);
  return (
    <li className="first:rounded-t-2xl last:rounded-b-2xl">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="ui-focus-ring grid w-full grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-3 gap-y-1 px-4 py-4 text-left hover:bg-surface-hover/50 focus-visible:-outline-offset-2 sm:flex sm:gap-4 sm:px-5"
      >
        <span
          title={actor.title}
          className="col-start-1 row-start-1 shrink-0 truncate text-xs text-muted sm:w-24 sm:text-sm"
        >
          {actor.label}
        </span>
        <span className="col-span-2 col-start-1 row-start-2 min-w-0 flex-1 text-sm leading-relaxed text-foreground">
          {(outcome === 'refused' || outcome === 'failed') && (
            <span className="mr-2 font-medium text-red-600 dark:text-red-400">
              {outcome === 'refused' ? 'Refused' : 'Failed'}
            </span>
          )}
          {describeAuditEntry(entry, definitions)}
        </span>
        <time
          dateTime={entry.created_at}
          title={exact}
          className="col-start-2 row-start-1 shrink-0 text-xs tabular-nums text-muted"
        >
          {describeWhen(entry.created_at)}
        </time>
      </button>
      {open && (
        <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-1 px-5 pb-4 text-xs text-gray-500 dark:text-gray-400">
          <dt>Action</dt>
          <dd className="font-mono text-gray-700 dark:text-gray-300">{entry.action}</dd>
          <dt>When</dt>
          <dd className="text-gray-700 dark:text-gray-300">{exact}</dd>
          <dt>From</dt>
          <dd className="font-mono text-gray-700 dark:text-gray-300">{entry.admin_ip}</dd>
          {entry.target_user_id && (
            <>
              <dt>User</dt>
              <dd className="font-mono text-gray-700 dark:text-gray-300">{entry.target_user_id}</dd>
            </>
          )}
          {entry.details && (
            <>
              <dt>Details</dt>
              <dd>
                <pre className="overflow-x-auto rounded-md bg-gray-50 px-3 py-2 font-mono text-[11px] text-gray-700 dark:bg-white/5 dark:text-gray-300">
                  {JSON.stringify(entry.details, null, 2)}
                </pre>
              </dd>
            </>
          )}
        </dl>
      )}
    </li>
  );
}
