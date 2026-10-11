'use client';

import Link from 'next/link';
import { useT } from '@/components/providers';
import { McpInvocationCard } from './McpInvocationCard';
import { useMcpApprovals } from './McpApprovalsProvider';

/** Visible with the task list collapsed and on every owner page. */
export function McpApprovalsLink({ current, className }: { current: boolean; className: string }) {
  const { invocations, hasMore, loaded } = useMcpApprovals();
  const t = useT();
  const count = `${invocations.length}${hasMore ? '+' : ''}`;
  return (
    <Link
      href="/approvals"
      aria-current={current ? 'page' : undefined}
      aria-label={loaded && invocations.length ? t('approvals.navCount', { count }) : undefined}
      className={className}
    >
      {t('approvals.title')}
      {loaded && invocations.length ? (
        <span
          aria-hidden="true"
          className="ml-2 rounded-full bg-amber-100 px-1.5 py-0.5 text-[11px] font-semibold text-amber-950 dark:bg-amber-500/20 dark:text-amber-200"
        >
          {count}
        </span>
      ) : null}
    </Link>
  );
}

export function McpApprovalsView() {
  const approvals = useMcpApprovals();
  const t = useT();
  return (
    <div className="mx-auto max-w-3xl px-4 py-8 sm:px-6">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">
          {t('approvals.title')}
        </h1>
        <button
          type="button"
          onClick={approvals.refresh}
          disabled={approvals.loading}
          className="rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-700 disabled:opacity-60 dark:border-white/10 dark:text-gray-300"
        >
          {t(approvals.loading ? 'approvals.refreshing' : 'approvals.refresh')}
        </button>
      </div>
      <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">{t('approvals.description')}</p>
      {approvals.error ? (
        <p role="alert" className="mt-4 text-sm text-red-600 dark:text-red-400">
          {approvals.error}
        </p>
      ) : null}
      {Object.entries(approvals.decisionErrors)
        .filter(
          ([id, message]) =>
            message && !approvals.invocations.some((invocation) => invocation.id === Number(id)),
        )
        .map(([id, message]) => (
          <p key={id} role="alert" className="mt-4 text-sm text-red-600 dark:text-red-400">
            {message}
          </p>
        ))}
      {approvals.lastDecision ? (
        <p role="status" className="mt-4 text-sm text-gray-700 dark:text-gray-300">
          {approvals.lastDecision.capability_name} · {approvals.lastDecision.status}
          {approvals.lastDecision.guidance ? `. ${approvals.lastDecision.guidance}` : ''}
        </p>
      ) : null}
      {!approvals.loaded && approvals.loading ? (
        <p role="status" className="mt-6 text-sm text-gray-500 dark:text-gray-400">
          {t('approvals.loading')}
        </p>
      ) : null}
      {approvals.loaded && !approvals.invocations.length && !approvals.hasMore ? (
        <p className="mt-6 rounded-xl border border-dashed border-gray-200 p-6 text-sm text-gray-500 dark:border-white/10 dark:text-gray-400">
          {t('approvals.empty')}
        </p>
      ) : null}
      {approvals.invocations.map((invocation) => (
        <section key={invocation.id} aria-label={invocation.thread_title ?? invocation.job_id}>
          <McpInvocationCard
            invocation={invocation}
            busy={approvals.decidingIds.has(invocation.id)}
            onDecision={(approve) => approvals.decide(invocation, approve)}
          >
            <div className="mb-2 flex items-center justify-between gap-3 text-xs text-amber-900 dark:text-amber-200">
              <span className="min-w-0 truncate font-medium">
                {invocation.thread_title ?? invocation.job_id}
              </span>
              <Link
                href={`/${encodeURIComponent(invocation.job_id)}`}
                className="shrink-0 rounded underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500"
              >
                {t('approvals.openConversation')}
              </Link>
            </div>
            {approvals.decisionErrors[invocation.id] ? (
              <p role="alert" className="mb-2 text-sm text-red-600 dark:text-red-400">
                {approvals.decisionErrors[invocation.id]}
              </p>
            ) : null}
          </McpInvocationCard>
        </section>
      ))}
      {approvals.hasMore ? (
        <button
          type="button"
          onClick={approvals.loadMore}
          disabled={approvals.loading}
          className="mt-4 rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-700 disabled:opacity-60 dark:border-white/10 dark:text-gray-300"
        >
          {t('approvals.loadMore')}
        </button>
      ) : null}
    </div>
  );
}
