import { NextResponse, type NextRequest } from 'next/server';

import {
  contentSecurityPolicy,
  createNonce,
  themeBootScriptHash,
} from '@/config/contentSecurityPolicy';

/**
 * Give each document its Content-Security-Policy and script nonce.
 *
 * The policy is set on the request as well as on the response. Next reads the
 * nonce from the request's `Content-Security-Policy` header while it renders
 * and sets it on every script it emits; the response header is the one the
 * browser enforces. Overwriting the request header also means a value a client
 * sent cannot choose the nonce. `contentSecurityPolicy.ts` explains the policy.
 */
export async function middleware(request: NextRequest) {
  const policy = contentSecurityPolicy({
    nonce: createNonce(),
    themeScriptHash: await themeBootScriptHash(),
    development: process.env.NODE_ENV === 'development',
  });
  const headers = new Headers(request.headers);
  headers.set('content-security-policy', policy);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set('content-security-policy', policy);
  return response;
}

export const config = {
  // Documents and RSC payloads; not the hashed build assets, which run no
  // policy of their own and would only pay for a nonce. The home page `/` is
  // listed on its own; `scripts/check-runtime-site-config.mjs` checks it.
  matcher: ['/', '/((?!_next/static|_next/image|favicon.ico).*)'],
};
