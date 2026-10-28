import { agentApiBase } from '@/config/env';
import { fetchWithAuth, jsonOrThrow, safeFetch } from './client';
import { APIError } from '@/lib/utils/errors';
import type { SessionUserApi } from './session';

export interface AuthConfig {
  mode: 'gateway' | 'local';
  signup_enabled: boolean;
}
export type WaitlistStatus = 'not_applied' | 'pending' | 'approved' | 'rejected' | 'revoked';
export interface WaitlistEntry {
  status: WaitlistStatus;
  purpose: string;
  review_reason: string | null;
  version: number;
  submitted_at: string | null;
  reviewed_at: string | null;
}
export interface AccountEntry {
  id: string;
  email: string;
  email_verified: boolean;
  status: 'active' | 'suspended';
  is_admin: boolean;
  waitlist: WaitlistEntry;
  created_at: string;
}
export type AccountAction =
  | 'approve'
  | 'reject'
  | 'revoke'
  | 'suspend'
  | 'restore'
  | 'grant_admin'
  | 'revoke_admin';

export async function getAuthConfig(signal?: AbortSignal): Promise<AuthConfig> {
  const response = await safeFetch(`${agentApiBase}/v1/auth/config`, { cache: 'no-store', signal });
  // Frontend may be rolled out ahead of the local-account backend.
  if (response.status === 404) return { mode: 'gateway', signup_enabled: false };
  const result = await jsonOrThrow<AuthConfig>(response);
  if (
    !result ||
    !['gateway', 'local'].includes(result.mode) ||
    typeof result.signup_enabled !== 'boolean'
  )
    throw new APIError('invalid_auth_config', 'Cannot load sign-in options.');
  return result;
}

// Auth failures must not emit AUTH_EXPIRED_EVENT for a different, already open session.
async function authPost<T>(path: string, body: object): Promise<T> {
  return jsonOrThrow<T>(
    await safeFetch(`${agentApiBase}/v1/auth/${path}`, {
      method: 'POST',
      credentials: 'include',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  );
}
export const loginAccount = (email: string, password: string) =>
  authPost<SessionUserApi>('login', { email, password });
export const signupAccount = (email: string, password: string) =>
  authPost<{ ok: boolean }>('signup', { email, password });
export const verifyAccountEmail = (token: string) =>
  authPost<{ ok: boolean }>('verify-email', { token });
export const resendVerification = (email: string) =>
  authPost<{ ok: boolean }>('resend-verification', { email });
export const forgotPassword = (email: string) =>
  authPost<{ ok: boolean }>('forgot-password', { email });
export const resetPassword = (token: string, password: string) =>
  authPost<{ ok: boolean }>('reset-password', { token, password });

export async function getWaitlist(signal?: AbortSignal): Promise<WaitlistEntry> {
  return jsonOrThrow(
    await fetchWithAuth(agentApiBase, '/v1/waitlist', { cache: 'no-store', signal }),
  );
}
export async function submitApplication(purpose: string): Promise<WaitlistEntry> {
  return jsonOrThrow(
    await fetchWithAuth(agentApiBase, '/v1/waitlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ purpose }),
    }),
  );
}
export async function listAccounts(
  status: WaitlistStatus | '',
  offset: number,
  signal?: AbortSignal,
): Promise<{ accounts: AccountEntry[]; total: number }> {
  const query = new URLSearchParams({ limit: '50', offset: String(offset) });
  if (status) query.set('status', status);
  return jsonOrThrow(
    await fetchWithAuth(agentApiBase, `/v1/admin/accounts?${query}`, { cache: 'no-store', signal }),
  );
}
export async function updateAccount(
  account: AccountEntry,
  action: AccountAction,
  reason: string,
): Promise<AccountEntry> {
  return jsonOrThrow(
    await fetchWithAuth(agentApiBase, `/v1/admin/accounts/${encodeURIComponent(account.id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, reason, expected_version: account.waitlist.version }),
    }),
  );
}
