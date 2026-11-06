'use client';

// Admin → Audit log: who did what, to whom, with what result — newest first.
//
// One sentence per action, who did it and when; the dotted action, the
// caller's address and the raw details open under it for anyone checking the
// record itself. By default only people are shown: administrators and access
// that belongs to no account. Automation, the control plane's own policy and
// accounts changing themselves are one switch away. `?user=` narrows the page
// to one account and what it owns; `?target_type=&target_id=` to one object.

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type {
  AdminAuditCategory,
  AdminAuditLogEntry,
  AdminAuditLogResponse,
  HostSettingDefinition,
} from '@/lib/api/admin';
import { getAgentRunnerHosts, listAdminAuditLog } from '@/lib/api/admin';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { dateTime } from '../runs/shared';
import {
  accountName,
  auditOutcome,
  describeActor,
  describeAuditEntry,
  describeResult,
  describeWhen,
} from './describe';

const FILTERS: { key: AdminAuditCategory | null; label: string }[] = [
  { key: null, label: 'All' },
  { key: 'accounts', label: 'Accounts' },
  { key: 'settings', label: 'Settings' },
  { key: 'servers', label: 'Servers' },
  { key: 'sandboxes', label: 'Sandboxes' },
  { key: 'measurements', label: 'Measurements' },
  { key: 'sensitive_access', label: 'Data access' },
  { key: 'access_control', label: 'Sign-up rules' },
];

const AUDIT = '/admin/audit';

/** What the page is narrowed to, read from its address. */
interface Scope {
  user: string | null;
  target: { type: string; id: string } | null;
}

function historyOf(query: Record<string, string>): string {
  return `${AUDIT}?${new URLSearchParams(query).toString()}`;
}

export default function AdminAuditLogPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
    return <AccessDenied />;
  }
  return (
    <Suspense fallback={null}>
      <ScopedAuditLog userId={state.user?.id ?? null} />
    </Suspense>
  );
}

function ScopedAuditLog({ userId }: { userId: string | null }) {
  const params = useSearchParams();
  const user = params.get('user');
  const type = params.get('target_type');
  const id = params.get('target_id');
  const scope: Scope = { user, target: type && id ? { type, id } : null };
  // A new scope is a new list: its own filter, cursor and open row.
  return <AuditLogView key={`${userId}|${user}|${type}|${id}`} userId={userId} scope={scope} />;
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

function AuditLogView({ userId, scope }: { userId: string | null; scope: Scope }) {
  const [category, setCategory] = useState<AdminAuditCategory | null>(null);
  // One account's history is the whole of it: what administrators did and
  // what the account did itself.
  const [everyone, setEveryone] = useState(scope.user !== null);
  const [entries, setEntries] = useState<AdminAuditLogEntry[] | null>(null);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [failures, setFailures] = useState<AdminAuditLogResponse['write_failures'] | null>(null);
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

  const { user, target } = scope;
  const targetType = target?.type ?? null;
  const targetId = target?.id ?? null;
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
        const page = await listAdminAuditLog(
          {
            category,
            actors: everyone ? 'all' : 'people',
            user,
            target: targetType && targetId ? { type: targetType, id: targetId } : null,
            before,
          },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setEntries((current) =>
          before === null ? page.entries : [...(current ?? []), ...page.entries],
        );
        setNextBefore(page.next_before);
        setFailures(page.write_failures ?? null);
      } catch (cause) {
        if (controller.signal.aborted) return;
        fail(cause);
        if (before === null) setEntries([]);
      } finally {
        if (!controller.signal.aborted) setLoadingMore(false);
      }
    },
    [category, everyone, user, targetType, targetId, fail],
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
          description="Who did what, to whom, and what came of it."
        />
        <AdminNavigation current="audit" />

        <div className="mt-8 max-w-4xl">
          <ScopeNotice scope={scope} entries={entries} />
          <Filters
            category={category}
            onCategory={setCategory}
            everyone={everyone}
            onEveryone={setEveryone}
          />
          {failures && failures.count > 0 && <WriteFailures failures={failures} />}

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

          <Entries
            entries={entries}
            error={error}
            userId={userId}
            definitions={definitions}
            open={open}
            onToggle={(id) => setOpen(open === id ? null : id)}
          />

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

function Filters({
  category,
  onCategory,
  everyone,
  onEveryone,
}: {
  category: AdminAuditCategory | null;
  onCategory: (category: AdminAuditCategory | null) => void;
  everyone: boolean;
  onEveryone: (everyone: boolean) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div
        role="group"
        aria-label="Show"
        className="inline-flex flex-wrap gap-1 self-start rounded-xl border border-border bg-surface-muted p-1"
      >
        {FILTERS.map((item) => (
          <button
            key={item.label}
            type="button"
            aria-pressed={item.key === category}
            onClick={() => onCategory(item.key)}
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
      <label className="flex min-h-11 items-center gap-2 text-sm text-muted">
        <input
          type="checkbox"
          checked={everyone}
          onChange={(event) => onEveryone(event.target.checked)}
          className="h-4 w-4"
        />
        Include automation, the system and accounts changing themselves
      </label>
    </div>
  );
}

/** What the page is narrowed to, and the way back to all of it. */
function ScopeNotice({ scope, entries }: { scope: Scope; entries: AdminAuditLogEntry[] | null }) {
  if (scope.user === null && scope.target === null) return null;
  let about: string;
  if (scope.user !== null) {
    // The newest row about the account names it as the trail knows it.
    const named = entries?.find((entry) => entry.target_user_id === scope.user);
    about = accountName(scope.user, named?.subject_label ?? null, named?.subject_deleted);
  } else {
    about = `${scope.target?.type} ${scope.target?.id}`;
  }
  return (
    <p className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-foreground">
      <span>
        Showing only <span className="font-medium">{about}</span>
      </span>
      <Link href={AUDIT} className="font-medium text-muted underline hover:text-foreground">
        Show everything
      </Link>
    </p>
  );
}

function WriteFailures({ failures }: { failures: AdminAuditLogResponse['write_failures'] }) {
  return (
    <p
      role="status"
      className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-300"
    >
      {failures.count === 1 ? 'One action' : `${failures.count} actions`} in the last 7 days could
      not be recorded here; they went ahead anyway
      {failures.latest_at ? `, most recently ${dateTime(failures.latest_at)}` : ''}. The control
      plane&apos;s log has each one as <code>agent_admin_audit_write_failed</code>.
    </p>
  );
}

function Entries({
  entries,
  error,
  userId,
  definitions,
  open,
  onToggle,
}: {
  entries: AdminAuditLogEntry[] | null;
  error: string | null;
  userId: string | null;
  definitions: HostSettingDefinition[];
  open: number | null;
  onToggle: (id: number) => void;
}) {
  if (entries === null) {
    return (
      <p role="status" className="mt-8 text-sm text-gray-500 dark:text-gray-400">
        Loading…
      </p>
    );
  }
  if (entries.length === 0) {
    return error ? null : (
      <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">Nothing recorded here yet.</p>
    );
  }
  return (
    <ul className="ui-card mt-6 divide-y divide-border overflow-hidden rounded-2xl">
      {entries.map((entry) => (
        <AuditRow
          key={entry.id}
          entry={entry}
          userId={userId}
          definitions={definitions}
          open={open === entry.id}
          onToggle={() => onToggle(entry.id)}
        />
      ))}
    </ul>
  );
}

const TONE: Record<string, string> = {
  alert: 'font-medium text-red-600 dark:text-red-400',
  quiet: 'text-gray-400 dark:text-gray-500',
};

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
  const actor = describeActor(entry, userId);
  const outcome = auditOutcome(entry);
  const result = describeResult(entry);
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
          className={`col-start-1 row-start-1 shrink-0 truncate text-xs sm:w-40 sm:text-sm ${
            actor.tone ? TONE[actor.tone] : 'text-muted'
          }`}
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
          {result && (
            <span
              className={`ml-2 text-xs ${
                entry.result === 'failed' ? 'text-red-600 dark:text-red-400' : 'text-muted'
              }`}
            >
              · {result}
            </span>
          )}
        </span>
        <time
          dateTime={entry.created_at}
          title={dateTime(entry.created_at)}
          className="col-start-2 row-start-1 shrink-0 text-xs tabular-nums text-muted"
        >
          {describeWhen(entry.created_at)}
        </time>
      </button>
      {open && <RowDetails entry={entry} />}
    </li>
  );
}

const VALUE = 'text-gray-700 dark:text-gray-300';
const CODE = `font-mono ${VALUE}`;

function RowDetails({ entry }: { entry: AdminAuditLogEntry }) {
  const subject = entry.target_user_id;
  return (
    <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-1 px-5 pb-4 text-xs text-gray-500 dark:text-gray-400">
      <dt>Action</dt>
      <dd className={CODE}>{entry.action}</dd>
      <dt>When</dt>
      <dd className={VALUE}>{dateTime(entry.created_at)}</dd>
      <dt>By</dt>
      <dd className={CODE}>{entry.actor_id}</dd>
      <dt>From</dt>
      <dd className={CODE}>{entry.admin_ip}</dd>
      {subject && (
        <>
          <dt>User</dt>
          <dd className={VALUE}>
            <span className="font-mono">{subject}</span>
            {entry.subject_label && ` · ${entry.subject_label}`}
            {' · '}
            <Link href={historyOf({ user: subject })} className="underline">
              Show only this account
            </Link>
          </dd>
        </>
      )}
      <dt>Acted on</dt>
      <dd className={VALUE}>
        <span className="font-mono">
          {entry.target_type} {entry.target_id}
        </span>
        {entry.target_type !== 'account' && entry.target_type !== 'unknown' && (
          <>
            {' · '}
            <Link
              href={historyOf({ target_type: entry.target_type, target_id: entry.target_id })}
              className="underline"
            >
              Show only this
            </Link>
          </>
        )}
      </dd>
      {entry.reason && (
        <>
          <dt>Reason</dt>
          <dd className={VALUE}>{entry.reason}</dd>
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
  );
}
