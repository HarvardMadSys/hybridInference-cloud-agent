'use client';

import Link from 'next/link';
import { useState } from 'react';

import ApplicationSettings from '../(agents)/admin/settings/ApplicationSettings';

export default function SetupPage() {
  const [draft, setDraft] = useState('');
  const [token, setToken] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [denied, setDenied] = useState(false);

  const lock = () => {
    setToken(null);
    setDraft('');
  };

  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6 sm:py-12">
      <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-50">
        Administrator setup
      </h1>
      <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">
        Configure the deployment before Gateway sign-in is available. Use the administrator recovery
        token supplied when this service was deployed.
      </p>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        The token is kept only while this page is open. Reloading or locking setup clears access.
      </p>
      {token ? (
        <>
          <div className="mt-5 flex flex-wrap items-center gap-3 text-sm">
            <button type="button" className="ui-button ui-button-secondary" onClick={lock}>
              Lock setup
            </button>
            <Link href="/login" className="ui-button ui-button-ghost">
              Go to sign-in
            </Link>
          </div>
          <ApplicationSettings
            key={attempt}
            recoveryToken={token}
            onAccessDenied={() => {
              lock();
              setDenied(true);
            }}
          />
        </>
      ) : (
        <form
          className="ui-card mt-6 max-w-lg p-5 sm:p-6"
          onSubmit={(event) => {
            event.preventDefault();
            if (!draft.trim()) return;
            setToken(draft.trim());
            setDraft('');
            setAttempt((previous) => previous + 1);
            setDenied(false);
          }}
        >
          <label htmlFor="recovery-token" className="text-sm font-medium">
            Administrator recovery token
          </label>
          <input
            id="recovery-token"
            type="password"
            required
            autoComplete="off"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            className="ui-input mt-2 w-full"
          />
          <button type="submit" className="ui-button ui-button-primary mt-4 w-full sm:w-auto">
            Unlock setup
          </button>
          {denied && (
            <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
              The recovery token was not accepted.
            </p>
          )}
        </form>
      )}
    </main>
  );
}
