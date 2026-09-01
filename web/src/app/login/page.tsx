'use client';

// The sign-in entry point.
//
// **Outside the `(agents)` route group on purpose**, so it is not behind the
// shell's `ProtectedRoute` — a login page that requires a session is a
// redirect loop.
//
// It exists because the alternative is worse, not because a click is nice to
// have. Without it an unauthenticated visitor is bounced through two redirects
// into somebody else's consent screen, having never been told which service
// they are about to authenticate against or what this one is. That reads as a
// hijack; it also gives a deployment whose sign-in is misconfigured nowhere to
// say so.
//
// Deliberately plain. Product copy and a public landing page are a separate
// piece of work; this is the smallest page that names the app, names the
// authority, and gets out of the way.

import { Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { config, gatewayUrl } from '@/config/env';
import { signInUrl } from '@/lib/auth/signin';

/** The sign-in authority, named by host so a self-hosted deployment reads true. */
function authorityName(): string {
  if (!gatewayUrl) return '';
  try {
    return new URL(gatewayUrl).host;
  } catch {
    return '';
  }
}

function LoginContent() {
  const params = useSearchParams();
  // Attacker-influenced: it survives a round trip through another origin. The
  // control plane reduces it to a same-site path again on the way in, which is
  // the copy that counts.
  const returnTo = params?.get('return_to') || '/';
  const unavailable = params?.has('sign_in_error') ?? false;
  const authority = authorityName();

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 px-6">
      <div className="w-full max-w-sm text-center">
        <h1 className="text-2xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          {config.appName}
        </h1>
        <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
          Run coding agents in sandboxed containers against your repositories.
        </p>

        {unavailable ? (
          <p
            role="alert"
            className="mt-6 rounded-lg bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200"
          >
            Sign-in is not configured for this deployment. Ask whoever runs it to set the identity
            provider.
          </p>
        ) : (
          <>
            {/* A plain link, not a button with a handler: the destination is a
                server route that answers with a redirect, and a full
                navigation is what it expects. */}
            <a
              href={signInUrl(returnTo)}
              className="mt-6 inline-flex min-h-11 w-full items-center justify-center rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-gray-800 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white"
            >
              Sign in{authority ? ` with ${authority}` : ''}
            </a>
            <p className="mt-4 text-xs text-gray-400 dark:text-gray-500">
              {authority
                ? `You will be sent to ${authority} to authorize this app. It never sees your password.`
                : 'You will be sent to your identity provider to authorize this app.'}
            </p>
          </>
        )}
      </div>
    </main>
  );
}

export default function LoginPage() {
  // `useSearchParams` needs a Suspense boundary or the whole route opts out of
  // static rendering at build time.
  return (
    <Suspense fallback={null}>
      <LoginContent />
    </Suspense>
  );
}
