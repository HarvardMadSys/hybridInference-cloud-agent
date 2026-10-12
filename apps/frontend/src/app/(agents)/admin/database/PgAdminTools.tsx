import { useEffect, useRef, useState, type FormEvent } from 'react';
import { getDatabaseTools, setupPgAdmin, type DatabaseTools } from '@/lib/api/adminDatabase';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { accessDenied, trackedRequest, type DatabaseAccessProps } from './requests';

export function PgAdminTools({ trackRequest, onDenied }: DatabaseAccessProps) {
  const [tools, setTools] = useState<DatabaseTools | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    const { controller, release } = trackedRequest(trackRequest);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let polls = 0;
    setLoading(true);
    setError(null);
    async function refresh() {
      if (controller.signal.aborted) return;
      try {
        const next = await getDatabaseTools(controller.signal);
        if (controller.signal.aborted) return;
        setTools(next);
        if (next.enabled && !next.available && !next.setup_required && polls++ < 40) {
          timer = setTimeout(() => void refresh(), 3000);
        }
      } catch (cause: unknown) {
        if (controller.signal.aborted) return;
        if (accessDenied(cause)) onDenied();
        else setError(getErrorMessage(cause));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
      release();
    };
  }, [trackRequest, onDenied, reload]);

  return (
    <div className="ui-card mt-6 max-w-2xl rounded-2xl p-4 sm:p-6">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-50">pgAdmin</h2>
      <p className="mt-3 text-sm leading-6 text-gray-600 dark:text-gray-400">
        Open pgAdmin for SQL queries and PostgreSQL server management.
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
          pgAdmin has not been enabled for this deployment. Contact your deployment administrator.
        </p>
      )}
      {tools?.enabled && tools.setup_required && (
        <SetupForm
          trackRequest={trackRequest}
          onCreated={(next) => {
            setTools(next);
            setStarting(true);
            setReload((value) => value + 1);
          }}
          onDenied={onDenied}
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
            className="ui-button ui-button-primary mt-6 min-h-11 px-5"
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
          className="ui-button ui-button-ghost mt-5 min-h-11 text-accent underline"
        >
          Check again
        </button>
      )}
    </div>
  );
}

function SetupForm({
  trackRequest,
  onCreated,
  onDenied,
  onConflict,
}: {
  trackRequest: DatabaseAccessProps['trackRequest'];
  onCreated: (tools: DatabaseTools) => void;
  onDenied: () => void;
  onConflict: () => void;
}) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<ReturnType<typeof trackedRequest> | null>(null);
  useEffect(
    () => () => {
      request.current?.controller.abort();
      request.current?.release();
    },
    [],
  );

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (request.current || creating) return;
    if (password !== confirmation) {
      setError('The passwords must match.');
      return;
    }
    const pending = trackedRequest(trackRequest);
    const { controller } = pending;
    request.current = pending;
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
      if (accessDenied(cause)) {
        onDenied();
      } else if (cause instanceof APIError && cause.statusCode === 409) {
        onConflict();
      } else {
        setError(getErrorMessage(cause));
      }
    } finally {
      pending.release();
      if (request.current === pending) request.current = null;
      if (!controller.signal.aborted) setCreating(false);
    }
  }

  const inputClass = 'ui-input mt-2 block min-h-11 w-full';
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
        className="ui-button ui-button-primary min-h-11 px-5"
      >
        {creating ? 'Setting up pgAdmin…' : 'Set up pgAdmin'}
      </button>
    </form>
  );
}
