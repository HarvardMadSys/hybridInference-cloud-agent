// The session BFF: this service's own half of sign-in.
//
// **The browser never holds a gateway credential at all.** Sign-in is two
// server routes — `GET /v1/session/login` and `GET /v1/session/callback` — and
// the browser only ever navigates to the first (see `@/lib/auth/signin`). The
// PKCE verifier lives in an HttpOnly cookie and the identity token is redeemed
// and spent inside the control plane, so neither is reachable from page
// JavaScript. What is left here are the two calls that read or drop the
// session cookie.

import { agentApiBase } from '@/config/env';
import { jsonOrThrow, safeFetch } from './client';
import { APIError } from '@/lib/utils/errors';

export type AgentPermission = 'agent.use' | 'agent.admin';

export interface AgentAccessApi {
  allowed: boolean;
  permissions: AgentPermission[];
}

export interface SessionUserApi {
  id: string;
  agent_access?: AgentAccessApi;
}

/** Who the cookie says we are. A 401 here means "not signed in", not an error. */
export async function getSessionUser(): Promise<SessionUserApi | null> {
  const resp = await safeFetch(`${agentApiBase}/v1/session/me`, {
    credentials: 'include',
    cache: 'no-store',
  });
  if (resp.status === 401) return null;
  try {
    const me = await jsonOrThrow<SessionUserApi>(resp);
    if (!me || typeof me.id !== 'string' || !me.id) {
      throw new APIError('invalid_session_response', 'Invalid session response', resp.status);
    }
    return me;
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new APIError('invalid_session_response', 'Invalid session response', resp.status);
    }
    throw error;
  }
}

/** Drop the session cookie. Best-effort: the UI signs out either way. */
export async function endSession(): Promise<void> {
  await safeFetch(`${agentApiBase}/v1/session/logout`, {
    method: 'POST',
    credentials: 'include',
  });
}
