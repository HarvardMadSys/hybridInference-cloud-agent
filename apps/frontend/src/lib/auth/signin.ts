// Where sign-in sends the browser back to.

import { basePath } from '@/config/env';

/**
 * Only same-site paths, expressed the way the router speaks them.
 *
 * **Strip `basePath`.** A path captured from `window.location.pathname`
 * already carries the prefix, and the server appends it back when it builds
 * the landing URL. Handed over unchanged it is applied twice: signing in at
 * `/agents` returned to `/agents/agents`, which the app resolved as a job
 * whose id was `agents` and rendered as "No such agent job".
 *
 */
export function safeReturnTo(candidate: string | undefined): string {
  if (
    !candidate ||
    !candidate.startsWith('/') ||
    candidate.startsWith('//') ||
    /[\\\u0000-\u001f\u007f]/.test(candidate)
  )
    return '/';
  if (!basePath) return candidate;
  if (candidate === basePath) return '/';
  if (candidate.startsWith(`${basePath}/`)) return candidate.slice(basePath.length);
  return candidate;
}

/** The login page, returning here: another account signs in beside this one. */
export function accountSignInUrl(pathname: string | null): string {
  return `${basePath}/login?${new URLSearchParams({ return_to: safeReturnTo(pathname ?? '/') })}`;
}
