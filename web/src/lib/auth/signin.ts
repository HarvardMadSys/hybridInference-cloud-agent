// Starting the gateway sign-in round trip.
//
// Every security decision belongs to the gateway: which clients it federates
// to, which redirect URIs it accepts (exact match — the callback below must be
// listed verbatim in `IDENTITY_ALLOWED_REDIRECTS`), how long a code lives. All
// this side does is generate the PKCE pair, park the verifier, and navigate.

import {
  basePath,
  gatewayAuthorizeUrl,
  OAUTH_CLIENT_ID,
  resolvedGatewayAuthorizeUrl,
} from '@/config/env';
import { challengeFor, createState, createVerifier, rememberPending } from './pkce';

/**
 * The one path the gateway is allowed to send the browser back to, as the app
 * knows it internally. Mounted under a `basePath` the browser sees it prefixed
 * — see {@link callbackUri}.
 */
export const CALLBACK_PATH = '/callback';

/**
 * The redirect URI to hand the gateway, absolute and including any base path.
 *
 * **`basePath` is applied here by hand and has to be.** Next.js prefixes the
 * routes and links it generates; it does not rewrite a string we concatenate.
 * Under `basePath=/agents` the page really is served at `/agents/callback`,
 * so a URI built without the prefix names a path that does not exist — and
 * the gateway matches `IDENTITY_ALLOWED_REDIRECTS` as an exact string, so the
 * failure is a refusal at `/authorize` before the user ever reaches consent.
 *
 * Whatever this returns must appear verbatim in the gateway's allowlist.
 */
export function callbackUri(): string {
  return `${window.location.origin}${basePath}${CALLBACK_PATH}`;
}

/**
 * Only same-site paths, expressed the way the router speaks them.
 *
 * Two jobs, and the second one is not obvious.
 *
 * **Same-site.** A `returnTo` is attacker-influenced — it survives a round trip
 * through another origin — so anything that is not a single-slash-rooted path
 * goes to `/`.
 *
 * **Strip `basePath`.** Next's router prefixes what it is given, and a path
 * captured from `window.location.pathname` already carries the prefix. Handed
 * back unchanged it is applied twice: signing in at `/agents` returned to
 * `/agents/agents`, which the app resolved as a job whose id was `agents` and
 * rendered as "No such agent job". The mirror of {@link callbackUri}, which has
 * to *add* the prefix because it hands a URL to the gateway rather than to the
 * router.
 */
export function safeReturnTo(candidate: string | undefined): string {
  if (!candidate || !candidate.startsWith('/') || candidate.startsWith('//')) return '/';
  if (!basePath) return candidate;
  if (candidate === basePath) return '/';
  if (candidate.startsWith(`${basePath}/`)) return candidate.slice(basePath.length);
  return candidate;
}

/** Thrown when this deployment has no gateway to sign in against. */
export class SignInNotConfigured extends Error {}

/**
 * Build the gateway's `/authorize` URL for a fresh PKCE flow, remembering the
 * verifier so the callback can redeem the code.
 *
 * Returns the URL rather than navigating so it can be asserted on in a test —
 * the challenge being present is the property that matters, and a function
 * that only ever calls `location.assign` has nothing to check.
 */
export async function buildAuthorizeUrl(returnTo: string): Promise<string> {
  if (!gatewayAuthorizeUrl) {
    throw new SignInNotConfigured(
      'NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL is not set, so this deployment cannot sign anyone in.',
    );
  }
  const verifier = createVerifier();
  const state = createState();
  const redirectUri = callbackUri();

  rememberPending({ verifier, state, returnTo, createdAt: Date.now() });

  // Pass the imported value explicitly so deployment/test overrides remain a
  // single source of truth rather than the resolver closing over a different
  // module instance.
  const resolved = resolvedGatewayAuthorizeUrl(gatewayAuthorizeUrl);
  if (!resolved) {
    throw new SignInNotConfigured(
      'NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL is invalid, so this deployment cannot sign anyone in.',
    );
  }
  const url = new URL(resolved);
  url.searchParams.set('client_id', OAUTH_CLIENT_ID);
  url.searchParams.set('redirect_uri', redirectUri);
  url.searchParams.set('code_challenge', await challengeFor(verifier));
  url.searchParams.set('code_challenge_method', 'S256');
  url.searchParams.set('state', state);
  return url.toString();
}

/** Send the browser to the gateway's consent page. */
export async function beginSignIn(returnTo: string): Promise<void> {
  const url = await buildAuthorizeUrl(returnTo);
  // A full navigation, not a router push: the destination is another origin.
  window.location.assign(url);
}
