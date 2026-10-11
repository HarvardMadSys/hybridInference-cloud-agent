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
    <main className="mx-auto w-full max-w-5xl px-6 py-12">
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
          <div className="mt-5 flex items-center gap-5 text-sm">
            <button type="button" className="underline" onClick={lock}>
              Lock setup
            </button>
            <Link href="/login" className="underline">
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
          className="mt-6 max-w-lg rounded-xl border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-900"
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
            className="mt-2 w-full rounded-md border border-gray-200 bg-transparent px-3 py-2 dark:border-white/15"
          />
          <button
            type="submit"
            className="mt-4 rounded-md bg-crimson px-4 py-2 text-sm font-medium text-white"
          >
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
