'use client';

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { getDatabaseTools, setupPgAdmin, type DatabaseTools } from '@/lib/api/adminDatabase';
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
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    if (denied) return;
    const request = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let polls = 0;
    setLoading(true);
    setError(null);
    async function refresh() {
      try {
        const next = await getDatabaseTools(request.signal);
        if (request.signal.aborted) return;
        setTools(next);
        if (next.enabled && !next.available && !next.setup_required && polls++ < 40) {
          timer = setTimeout(() => void refresh(), 3000);
        }
      } catch (cause: unknown) {
        if (request.signal.aborted) return;
        if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
          setTools(null);
          setDenied(true);
        } else {
          setError(getErrorMessage(cause));
        }
      } finally {
        if (!request.signal.aborted) setLoading(false);
      }
    }
    void refresh();
    return () => {
      request.abort();
      clearTimeout(timer);
    };
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
          {tools?.enabled && tools.setup_required && (
            <SetupForm
              onCreated={(next) => {
                setTools(next);
                setStarting(true);
                setReload((value) => value + 1);
              }}
              onDenied={() => setDenied(true)}
              onConflict={() => setReload((value) => value + 1)}
            />
          )}
          {tools?.enabled && !tools.available && !tools.setup_required && (
            <p role="status" className="mt-5 text-sm text-amber-700 dark:text-amber-400">
              {starting
                ? 'Starting pgAdmin… This page will update when it is ready.'
                : 'pgAdmin is temporarily unavailable. Try again shortly.'}
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
          {!loading && (error || (tools && !tools.available && !tools.setup_required)) && (
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

function SetupForm({
  onCreated,
  onDenied,
  onConflict,
}: {
  onCreated: (tools: DatabaseTools) => void;
  onDenied: () => void;
  onConflict: () => void;
}) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (creating) return;
    if (password !== confirmation) {
      setError('The passwords must match.');
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setCreating(true);
    setError(null);
    try {
      const next = await setupPgAdmin(email, password, controller.signal);
      if (controller.signal.aborted) return;
      setPassword('');
      setConfirmation('');
      onCreated(next);
    } catch (cause: unknown) {
      if (controller.signal.aborted) return;
      if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
        onDenied();
      } else if (cause instanceof APIError && cause.statusCode === 409) {
        onConflict();
      } else {
        setError(getErrorMessage(cause));
      }
    } finally {
      if (!controller.signal.aborted) setCreating(false);
    }
  }

  const inputClass =
    'mt-1 block w-full rounded-lg border border-gray-300 bg-transparent px-3 py-2 text-gray-900 focus:border-crimson focus:outline-none focus:ring-1 focus:ring-crimson dark:border-white/20 dark:text-gray-50';
  return (
    <form onSubmit={(event) => void submit(event)} className="mt-6 space-y-4">
      <p className="text-sm leading-6 text-gray-600 dark:text-gray-400">
        Set up pgAdmin for the first time. Choose the administrator account you will use to sign in.
      </p>
      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
        Administrator email
        <input
          type="email"
          autoComplete="username"
          required
          maxLength={254}
          disabled={creating}
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className={inputClass}
        />
      </label>
      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
        Password
        <input
          type="password"
          autoComplete="new-password"
          required
          minLength={12}
          maxLength={256}
          disabled={creating}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          aria-describedby="pgadmin-password-help"
          className={inputClass}
        />
      </label>
      <p id="pgadmin-password-help" className="text-xs text-gray-500 dark:text-gray-400">
        Use at least 12 characters.
      </p>
      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
        Confirm password
        <input
          type="password"
          autoComplete="new-password"
          required
          minLength={12}
          maxLength={256}
          disabled={creating}
          value={confirmation}
          onChange={(event) => setConfirmation(event.target.value)}
          className={inputClass}
        />
      </label>
      {error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={creating}
        className="inline-flex min-h-10 items-center rounded-lg bg-crimson px-4 py-2 text-sm font-semibold text-white hover:bg-crimson/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson focus-visible:ring-offset-2 disabled:opacity-50 dark:focus-visible:ring-offset-gray-900"
      >
        {creating ? 'Setting up pgAdmin…' : 'Set up pgAdmin'}
      </button>
    </form>
  );
}
