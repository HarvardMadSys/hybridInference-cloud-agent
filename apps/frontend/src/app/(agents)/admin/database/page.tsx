'use client';

import { useEffect, useState } from 'react';
import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { getDatabaseTools, type DatabaseTools } from '@/lib/api/adminDatabase';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../AdminNavigation';

export default function AdminDatabasePage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AccessDenied />;
  return <DatabaseView key={state.user?.id ?? 'admin'} />;
}

function AccessDenied() {
  return (
    <div className="flex flex-1 items-center justify-center p-6 text-sm text-gray-500 dark:text-gray-400">
      Admin access required.
    </div>
  );
}

function DatabaseView() {
  const [tools, setTools] = useState<DatabaseTools | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    if (denied) return;
    const request = new AbortController();
    setLoading(true);
    setTools(null);
    setError(null);
    void getDatabaseTools(request.signal)
      .then((next) => {
        if (!request.signal.aborted) setTools(next);
      })
      .catch((cause: unknown) => {
        if (request.signal.aborted) return;
        if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
          setTools(null);
          setDenied(true);
        } else {
          setError(getErrorMessage(cause));
        }
      })
      .finally(() => {
        if (!request.signal.aborted) setLoading(false);
      });
    return () => request.abort();
  }, [denied, reload]);

  if (denied) return <AccessDenied />;

  return (
    <div className="flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-7xl px-6 py-12 lg:px-12 lg:py-16">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="database" />
        <div className="mt-8 max-w-2xl rounded-2xl border border-gray-200 bg-white p-6 dark:border-white/10 dark:bg-gray-900">
          <p className="text-xs font-semibold uppercase tracking-wider text-crimson dark:text-crimson-light">
            PostgreSQL
          </p>
          <h2 className="mt-2 text-xl font-semibold text-gray-900 dark:text-gray-50">pgAdmin</h2>
          <p className="mt-3 text-sm leading-6 text-gray-600 dark:text-gray-400">
            Browse database tables, run SQL queries, and manage your PostgreSQL servers.
          </p>
          {loading && (
            <p role="status" className="mt-5 text-sm text-gray-500 dark:text-gray-400">
              Checking pgAdmin…
            </p>
          )}
          {error && (
            <p role="alert" className="mt-5 text-sm text-red-700 dark:text-red-400">
              {error}
            </p>
          )}
          {tools?.enabled === false && (
            <p role="status" className="mt-5 text-sm text-gray-600 dark:text-gray-400">
              pgAdmin has not been enabled for this deployment. Contact your deployment
              administrator.
            </p>
          )}
          {tools?.enabled && !tools.available && (
            <p role="status" className="mt-5 text-sm text-amber-700 dark:text-amber-400">
              pgAdmin is temporarily unavailable. Try again shortly.
            </p>
          )}
          {tools?.available && tools.url && (
            <>
              <a
                href={tools.url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-6 inline-flex min-h-10 items-center gap-2 rounded-lg bg-crimson px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-crimson/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-900"
              >
                Open pgAdmin
                <span aria-hidden="true">↗</span>
              </a>
              <p className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">
                Opens in a new tab. Sign in with your pgAdmin account; database connections use your
                PostgreSQL credentials.
              </p>
            </>
          )}
          {!loading && (error || (tools && !tools.available)) && (
            <button
              type="button"
              onClick={() => setReload((value) => value + 1)}
              className="mt-5 rounded text-sm font-medium text-crimson underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:text-crimson-light"
            >
              Check again
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
