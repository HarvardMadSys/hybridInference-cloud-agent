// The session: who the HttpOnly cookie says this browser is.
//
// Page JavaScript never holds the session token. Sign-in is in
// `@/lib/api/accounts`; what is left here are the two calls that read or drop
// the session cookie.

import { agentApiBase } from '@/config/env';
import { jsonOrThrow, safeFetch } from './client';
import type { AccountIdentity, WaitlistEntry } from './accounts';
import { APIError } from '@/lib/utils/errors';

export type AgentPermission = 'agent.use' | 'agent.admin';

export interface AgentAccessApi {
  allowed: boolean;
  permissions: AgentPermission[];
}

export interface SessionUserApi {
  id: string;
  email?: string;
  waitlist?: WaitlistEntry;
  agent_access?: AgentAccessApi;
  /** How the account signs in. */
  identity?: AccountIdentity;
  /** Whether it holds administration. */
  is_local_admin?: boolean;
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

/**
 * Drop the session cookie.
 *
 * Only the server's answer ends the session. A request that failed — an outage,
 * a proxy's error page, no network — has left this browser signed in, so it
 * is reported rather than treated as done.
 */
export async function endSession(): Promise<void> {
  const resp = await safeFetch(`${agentApiBase}/v1/session/logout`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!resp.ok) await jsonOrThrow(resp);
}
