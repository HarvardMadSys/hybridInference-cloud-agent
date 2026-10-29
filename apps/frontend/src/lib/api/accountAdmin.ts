// Local account administration: /v1/admin/accounts and /v1/admin/account-mail.
// Adapted from HybridInference's admin users, signup-domains, login-events and
// broadcast-email calls, over this service's own routes.
import { agentApiBase } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';
import type { AccountEntry, WaitlistStatus } from './accounts';

async function admin<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = init.body ? { 'Content-Type': 'application/json' } : undefined;
  return jsonOrThrow<T>(
    await fetchWithAuth(agentApiBase, path, { cache: 'no-store', ...init, headers }),
  );
}

export type AccountStatus = 'active' | 'suspended' | 'deleted';
export const saveAccountNotes = (
  account: AccountEntry,
  notes: { admin_note?: string; suspension_message?: string },
) =>
  admin<AccountEntry>(`/v1/admin/accounts/${encodeURIComponent(account.id)}/notes`, {
    method: 'PATCH',
    body: JSON.stringify({ ...notes, expected_version: account.waitlist.version }),
  });

export interface LoginEvent {
  method: string;
  outcome: 'success' | 'failure';
  failure_reason: string | null;
  ip: string;
  user_agent: string | null;
  created_at: string;
}
export const listLoginEvents = (accountId: string, signal?: AbortSignal) =>
  admin<{ events: LoginEvent[] }>(
    `/v1/admin/accounts/${encodeURIComponent(accountId)}/login-events`,
    { signal },
  );
export function purgeLoginEvents(target: { olderThanDays: number } | { accountId: string }) {
  const params = new URLSearchParams(
    'olderThanDays' in target
      ? { older_than_days: String(target.olderThanDays) }
      : { account_id: target.accountId },
  );
  return admin<{ deleted: number }>(`/v1/admin/accounts/login-events?${params}`, {
    method: 'DELETE',
  });
}
export interface ErasureResult {
  account: AccountEntry;
  deleted: Record<string, number>;
  retained_archives: string[];
}
export const eraseAccount = (accountId: string, confirm: string) =>
  admin<ErasureResult>(`/v1/admin/accounts/${encodeURIComponent(accountId)}/erase`, {
    method: 'POST',
    body: JSON.stringify({ confirm }),
  });

export interface SignupDomain {
  domain: string;
  is_wildcard: boolean;
  created_at: string;
  created_by: string;
}
export interface SignupDomains {
  domains: SignupDomain[];
  approval_policy: 'manual' | 'domains';
}
export const listSignupDomains = (signal?: AbortSignal) =>
  admin<SignupDomains>('/v1/admin/accounts/signup-domains', { signal });
export const addSignupDomain = (domain: string) =>
  admin<SignupDomains>('/v1/admin/accounts/signup-domains', {
    method: 'POST',
    body: JSON.stringify({ domain }),
  });
export const removeSignupDomain = (row: SignupDomain) =>
  admin<SignupDomains>(
    `/v1/admin/accounts/signup-domains/${encodeURIComponent(row.domain)}?wildcard=${row.is_wildcard}`,
    { method: 'DELETE' },
  );

export interface MailMessage {
  template_key: 'maintenance' | 'announcement' | null;
  template_vars: Record<string, string>;
  subject: string;
  body_markdown: string;
  target_statuses: AccountStatus[];
  target_admissions: WaitlistStatus[];
}
export interface MailPreview {
  recipient_count: number;
  subject: string;
  html: string;
  text: string;
}
export interface MailEntry {
  id: string;
  subject: string;
  status: 'scheduled' | 'sending' | 'sent' | 'failed' | 'cancelled';
  recipient_count: number;
  scheduled_at: string | null;
  sent_at: string | null;
  created_by: string;
  created_at: string;
  template_key: string | null;
  target_statuses: string[];
  target_admissions: string[];
}
export interface MailRecipient {
  account_id: string;
  email: string;
  status: 'pending' | 'sent' | 'failed' | 'skipped';
  error: string | null;
  sent_at: string | null;
}
export const previewMail = (message: MailMessage) =>
  admin<MailPreview>('/v1/admin/account-mail/preview', {
    method: 'POST',
    body: JSON.stringify(message),
  });
export const testMail = (message: MailMessage) =>
  admin<{ ok: boolean }>('/v1/admin/account-mail/test', {
    method: 'POST',
    body: JSON.stringify(message),
  });
export const createMail = (message: MailMessage, scheduledAt: string | null) =>
  admin<MailEntry>('/v1/admin/account-mail', {
    method: 'POST',
    body: JSON.stringify({ ...message, scheduled_at: scheduledAt }),
  });
export const listMail = (signal?: AbortSignal) =>
  admin<{ broadcasts: MailEntry[]; total: number }>('/v1/admin/account-mail', { signal });
export const mailDetail = (id: string, signal?: AbortSignal) =>
  admin<{ broadcast: MailEntry; recipients: MailRecipient[]; total_recipients: number }>(
    `/v1/admin/account-mail/${encodeURIComponent(id)}`,
    { signal },
  );
export const cancelMail = (id: string) =>
  admin<{ ok: boolean }>(`/v1/admin/account-mail/${encodeURIComponent(id)}`, { method: 'DELETE' });
