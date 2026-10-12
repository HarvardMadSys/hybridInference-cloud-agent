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
    <div lang="en" className="flex min-h-screen flex-col bg-surface-muted">
      <main className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center px-4 py-8">
        <section
          aria-labelledby="welcome-heading"
          className="rounded-xl border border-border bg-surface p-5"
        >
          <h1 className="text-sm font-semibold tracking-tight text-foreground">
            {branding.appName}
          </h1>
          <h2
            id="welcome-heading"
            className="mt-3 text-lg font-semibold tracking-tight text-foreground"
          >
            Sign in
          </h2>
          <p className="mt-1 text-sm leading-5 text-muted">
            Run coding agents in sandboxed containers against your repositories.
          </p>

          {hasError && (
            <p
              role="alert"
              className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
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
                className="mt-4 inline-flex min-h-9 w-full items-center justify-center gap-2 rounded-md bg-accent px-3 py-2 text-center text-sm font-medium text-accent-foreground transition hover:bg-accent-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
              >
                <span className="min-w-0 break-words">
                  {expired
                    ? 'Start again'
                    : temporary
                      ? 'Try again'
                      : `Sign in${authority ? ` with ${authority}` : ''}`}
                </span>
              </a>
              <p className="mt-3 break-words text-xs leading-5 text-muted">
                {authority
                  ? `You will be sent to ${authority} to authorize this app. It never sees your password.`
                  : 'You will be sent to your identity provider to authorize this app.'}
              </p>
            </>
          )}
          <p className="mt-4 text-xs text-muted">
            Service administrators:{' '}
            <Link href="/setup" className="underline">
              configure missing settings
            </Link>
            .
          </p>
        </section>
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
