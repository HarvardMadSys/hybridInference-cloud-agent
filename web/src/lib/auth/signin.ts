// Starting the gateway sign-in round trip.
//
// **This module hands the browser to the control plane and stops.** Every
// security decision — the PKCE pair, the CSRF `state`, the redirect URI the
// gateway matches, the code exchange — belongs to the server side of this
// service (`cloud_agent/api/session.py`), and the browser is deliberately not
// trusted with any of it.
//
// It used to be. An earlier revision minted the verifier here, parked it in
// `localStorage`, and let the callback page redeem the code for the gateway's
// identity token. Both of those are bearer credentials sitting in page
// JavaScript, one XSS away from being someone else's; the BFF exists precisely
// so the browser holds nothing but an HttpOnly cookie it cannot read.

import { agentApiBase, basePath } from '@/config/env';

/**
 * Only same-site paths, expressed the way the router speaks them.
 *
 * **Strip `basePath`.** A path captured from `window.location.pathname`
 * already carries the prefix, and the server appends it back when it builds
 * the landing URL. Handed over unchanged it is applied twice: signing in at
 * `/agents` returned to `/agents/agents`, which the app resolved as a job
 * whose id was `agents` and rendered as "No such agent job".
 *
 * The same-site check is repeated on the server, which is where it counts —
 * this value survives a round trip through another origin, so the copy that
 * matters is the one an attacker cannot skip.
 */
export function safeReturnTo(candidate: string | undefined): string {
  if (!candidate || !candidate.startsWith('/') || candidate.startsWith('//')) return '/';
  if (!basePath) return candidate;
  if (candidate === basePath) return '/';
  if (candidate.startsWith(`${basePath}/`)) return candidate.slice(basePath.length);
  return candidate;
}

/**
 * The control plane's sign-in entry point, with the landing path attached.
 *
 * Returned rather than navigated to so it can be asserted on in a test — a
 * function that only ever calls `location.assign` has nothing to check.
 */
export function signInUrl(returnTo: string): string {
  // An empty `agentApiBase` is not a missing one: it is the same-origin
  // deployment (DR4), and the result is a relative URL the browser resolves
  // against the page. There is nothing here to refuse.
  const params = new URLSearchParams({ return_to: safeReturnTo(returnTo) });
  return `${agentApiBase}/v1/session/login?${params.toString()}`;
}

/** Send the browser to the control plane, which sends it on to the gateway. */
export async function beginSignIn(returnTo: string): Promise<void> {
  // A full navigation, not a router push: the destination is a server route,
  // and the redirect chain that follows leaves this origin.
  window.location.assign(signInUrl(returnTo));
}
