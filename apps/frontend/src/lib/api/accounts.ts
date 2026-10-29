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
export interface AccountIdentity {
  locale: string | null;
  /** When the account's language was chosen. */
  locale_chosen_at?: string | null;
  email: string | null;
  email_verified: boolean;
  login_name: string | null;
  phone: string | null;
  has_password: boolean;
  identity_verified: boolean;
  user_name: string | null;
  label: string;
}
export interface AccountEntry {
  id: string;
  email: string | null;
  email_verified: boolean;
  status: 'active' | 'suspended' | 'deleted';
  is_admin: boolean;
  waitlist: WaitlistEntry;
  created_at: string;
  identity?: AccountIdentity | null;
  admin_note?: string | null;
  suspension_message?: string | null;
  last_login_at?: string | null;
  deleted_at?: string | null;
  erased_at?: string | null;
  reviewed_by?: string | null;
}
export type AccountAction =
  | 'approve'
  | 'reject'
  | 'revoke'
  | 'suspend'
  | 'restore'
  | 'grant_admin'
  | 'revoke_admin'
  | 'delete'
  | 'resume';

/** The part of a session answer a sign-in page acts on. */
export interface SessionUserApiLike {
  id: string;
  agent_access?: { allowed?: boolean } | null;
}

/** What a local deployment's account pages offer (`GET /v1/auth/options`). */
export interface AuthOptions {
  signup_enabled: boolean;
  phone_signup_enabled: boolean;
  email_delivery: boolean;
  require_email_verification: boolean;
  phone: { enabled: boolean; default_country_code: string; allowed_prefixes: string[] };
  turnstile_site_key: string;
  terms_url: string;
  terms_text: string;
  setup_required: boolean;
  max_accounts_per_browser: number;
}
export type PhonePurpose =
  | 'signup'
  | 'login'
  | 'bind'
  | 'delete_account'
  | 'bind_email'
  | 'set_password';
export interface BrowserAccount {
  id: string;
  label: string;
  user_name: string | null;
  active: boolean;
  signed_in: boolean;
  agent_access: boolean | null;
}
export interface BrowserAccounts {
  active_account_id: string | null;
  accounts: BrowserAccount[];
  max_accounts: number;
}
/** What the consent and details step adds to an email sign-up. */
export interface SignupDetails {
  user_name?: string;
  use_case: string;
  accepted_tos: boolean;
  turnstile_token?: string;
}

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
/**
 * Sign in with an email or, for an account created without one, its login
 * name. The field keeps the name `email`, as HybridInference's does.
 */
export const loginAccount = (email: string, password: string) =>
  authPost<SessionUserApi>('login', { email, password });
export const signupAccount = (email: string, password: string, details?: SignupDetails) =>
  authPost<{ ok: boolean }>('signup', { email, password, ...details });
export const sendPhoneCode = (phone: string, purpose: PhonePurpose, turnstileToken?: string) =>
  authPost<{ ok: boolean; retry_after: number }>('phone/code', {
    phone,
    purpose,
    ...(turnstileToken ? { turnstile_token: turnstileToken } : {}),
  });
export const signupPhone = (details: {
  phone: string;
  code: string;
  user_name?: string;
  use_case: string;
  accepted_tos: boolean;
}) => authPost<{ ok: boolean; requires_approval: boolean }>('phone/signup', details);
export const loginPhone = (phone: string, code: string) =>
  authPost<SessionUserApi>('phone/login', { phone, code });
export const createSetupAdmin = (details: {
  setup_code: string;
  login_name: string;
  password: string;
  display_name?: string;
}) => authPost<SessionUserApi>('setup/admin', details);
export const switchAccount = (accountId: string) =>
  authPost<SessionUserApi>('switch', { account_id: accountId });
export const signOutEverywhereHere = () => authPost<{ ok: boolean }>('logout-all', {});

export async function getAuthOptions(signal?: AbortSignal): Promise<AuthOptions> {
  return jsonOrThrow<AuthOptions>(
    await safeFetch(`${agentApiBase}/v1/auth/options`, {
      credentials: 'include',
      cache: 'no-store',
      signal,
    }),
  );
}
export async function listBrowserAccounts(signal?: AbortSignal): Promise<BrowserAccounts> {
  return jsonOrThrow<BrowserAccounts>(
    await safeFetch(`${agentApiBase}/v1/auth/accounts`, {
      credentials: 'include',
      cache: 'no-store',
      signal,
    }),
  );
}
export async function removeBrowserAccount(accountId: string): Promise<BrowserAccounts> {
  return jsonOrThrow<BrowserAccounts>(
    await safeFetch(`${agentApiBase}/v1/auth/accounts/${encodeURIComponent(accountId)}`, {
      method: 'DELETE',
      credentials: 'include',
      cache: 'no-store',
    }),
  );
}
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
export interface AccountFilters {
  accountStatus?: 'active' | 'suspended' | 'deleted' | '';
  q?: string;
  sort?: 'created' | 'last_login';
}
export async function listAccounts(
  status: WaitlistStatus | '',
  offset: number,
  signal?: AbortSignal,
  filters: AccountFilters = {},
): Promise<{ accounts: AccountEntry[]; total: number; counts?: Record<string, number> }> {
  const query = new URLSearchParams({ limit: '50', offset: String(offset) });
  if (status) query.set('status', status);
  if (filters.accountStatus) query.set('account_status', filters.accountStatus);
  if (filters.q?.trim()) query.set('q', filters.q.trim());
  if (filters.sort && filters.sort !== 'created') query.set('sort', filters.sort);
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
