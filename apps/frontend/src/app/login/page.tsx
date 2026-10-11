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
// The sign-in authority stays visible before the visitor leaves this app.

import { Suspense } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { signInUrl } from '@/lib/auth/signin';
import { DeploymentFooter } from '@/components/DeploymentFooter';

/** The sign-in authority, named by host so a self-hosted deployment reads true. */
function authorityName(gatewayUrl: string): string {
  if (!gatewayUrl) return '';
  try {
    return new URL(gatewayUrl).host;
  } catch {
    return '';
  }
}

function LoginContent() {
  const params = useSearchParams();
  // Resolved on the server for this request. The page is outside the
  // `(agents)` group but inside the root layout, so the provider is above it.
  const { branding } = useSiteConfig();
  // Attacker-influenced: it survives a round trip through another origin. The
  // control plane reduces it to a same-site path again on the way in, which is
  // the copy that counts.
  const returnTo = params?.get('return_to') || '/';
  const signInError = params?.get('sign_in_error');
  const temporary = signInError === 'temporary';
  const expired = signInError === 'expired';
  const accountUnavailable = signInError === 'account_unavailable';
  const hasError = params?.has('sign_in_error') ?? false;
  const authority = authorityName(branding.gatewayUrl);

  return (
    <div className="relative isolate flex min-h-screen flex-col overflow-hidden bg-[#f8f7f4] dark:bg-gray-950">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -right-44 -top-52 -z-10 h-[620px] w-[620px] rounded-full border border-crimson/5 bg-gradient-to-br from-crimson/5 to-transparent dark:border-crimson/10 dark:from-crimson/10"
      />
      <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col justify-center px-6 py-12 sm:px-10 lg:py-20">
        <div className="grid items-center gap-12 lg:grid-cols-[1.15fr_1fr] lg:gap-20">
          <section>
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-crimson text-white shadow-sm">
                <svg
                  aria-hidden="true"
                  width="22"
                  height="22"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.75"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-12-2 14" />
                </svg>
              </span>
              <h1 className="text-lg font-semibold tracking-tight text-gray-900 dark:text-gray-50">
                {branding.appName}
              </h1>
            </div>

            <p className="mt-12 text-xs font-semibold uppercase tracking-[0.18em] text-crimson dark:text-crimson-light lg:mt-16">
              Your coding workspace
            </p>
            <h2 className="mt-4 max-w-lg font-serif text-4xl leading-[1.15] tracking-tight text-gray-900 sm:text-5xl dark:text-gray-50">
              From a task to
              <br />a pull request.
            </h2>
            <p className="mt-5 max-w-md text-base leading-7 text-gray-600 dark:text-gray-400">
              Run coding agents in sandboxed containers against your repositories.
            </p>

            <div className="mt-10 hidden max-w-md rounded-2xl border border-gray-200/80 bg-white/65 p-5 lg:block dark:border-gray-800 dark:bg-gray-900/50">
              <div className="flex items-center justify-between border-b border-gray-200/80 pb-4 dark:border-gray-800">
                <span className="text-xs font-medium text-gray-500 dark:text-gray-400">
                  A little less busywork. A little more building.
                </span>
                <svg
                  aria-hidden="true"
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="shrink-0 text-gray-400 dark:text-gray-500"
                >
                  <path d="m8 5-7 7 7 7m8-14 7 7-7 7" />
                </svg>
              </div>
              <ol className="mt-5 space-y-4">
                {['Choose your repository', 'Work with a coding agent', 'Review your changes'].map(
                  (step, index) => (
                    <li key={step} className="flex items-center gap-3">
                      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-gray-100 text-[10px] font-semibold text-gray-500 dark:bg-gray-800 dark:text-gray-400">
                        {String(index + 1).padStart(2, '0')}
                      </span>
                      <span className="text-sm text-gray-700 dark:text-gray-300">{step}</span>
                    </li>
                  ),
                )}
              </ol>
            </div>
          </section>

          <section
            aria-labelledby="welcome-heading"
            className="w-full rounded-3xl border border-gray-200/80 bg-white p-7 shadow-[0_12px_48px_-20px_rgba(17,24,39,0.2)] sm:p-9 dark:border-gray-800 dark:bg-gray-900 dark:shadow-none"
          >
            <span className="mb-6 flex h-12 w-12 items-center justify-center rounded-2xl border border-gray-200 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
              <svg
                aria-hidden="true"
                width="22"
                height="22"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <rect x="4" y="10" width="16" height="11" rx="3" />
                <path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2" />
              </svg>
            </span>
            <h2
              id="welcome-heading"
              className="text-2xl font-semibold tracking-tight text-gray-900 dark:text-gray-50"
            >
              Welcome to your workspace
            </h2>
            <p className="mt-2 text-sm leading-6 text-gray-500 dark:text-gray-400">
              Sign in to start building with your agents.
            </p>

            {hasError && (
              <p
                role="alert"
                className="mt-6 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
              >
                {expired
                  ? 'This sign-in has expired or is no longer valid. Please start again.'
                  : temporary
                    ? 'Sign-in is temporarily unavailable. Please try again shortly.'
                    : accountUnavailable
                      ? 'This account cannot sign in. Contact the service administrator for help.'
                      : 'Sign-in is not configured correctly for this deployment. Contact the service administrator.'}
              </p>
            )}
            {(!hasError || temporary || expired) && (
              <>
                {/* A plain link, not a button with a handler: the destination is a
                server route that answers with a redirect, and a full
                navigation is what it expects. */}
                <a
                  href={signInUrl(returnTo)}
                  className="group mt-7 inline-flex min-h-12 w-full items-center justify-center gap-3 rounded-xl bg-crimson px-5 py-3 text-center text-sm font-medium text-white shadow-sm transition hover:bg-crimson-dark focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson focus-visible:ring-offset-4 dark:focus-visible:ring-offset-gray-900"
                >
                  <span className="min-w-0 break-words">
                    {expired
                      ? 'Start again'
                      : temporary
                        ? 'Try again'
                        : `Sign in${authority ? ` with ${authority}` : ''}`}
                  </span>
                  <svg
                    aria-hidden="true"
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.75"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="shrink-0 transition-transform group-hover:translate-x-0.5 motion-reduce:transition-none"
                  >
                    <path d="M5 12h14m-5-5 5 5-5 5" />
                  </svg>
                </a>
                <p className="mt-5 break-words text-xs leading-5 text-gray-500 dark:text-gray-400">
                  {authority
                    ? `You will be sent to ${authority} to authorize this app. It never sees your password.`
                    : 'You will be sent to your identity provider to authorize this app.'}
                </p>
              </>
            )}
            <p className="mt-5 text-center text-xs text-gray-500 dark:text-gray-400">
              Service administrators:{' '}
              <Link href="/setup" className="underline">
                configure missing settings
              </Link>
              .
            </p>
            <div className="mt-7 flex items-center gap-2 border-t border-gray-100 pt-5 text-xs text-gray-500 dark:border-gray-800 dark:text-gray-400">
              <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-crimson/60" />
              Your repositories. Your workflow.
            </div>
          </section>
        </div>
      </main>
      <DeploymentFooter />
    </div>
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
