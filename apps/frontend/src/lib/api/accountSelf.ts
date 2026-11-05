// The signed-in local account's own changes: /v1/auth/account/*.
// Adapted from HybridInference's settings calls (change-password, email/bind,
// phone/bind, delete-account, preferences/locale). Each sensitive change is
// proven again as the account signs in: its password, or a code sent to its
// verified phone when it has none.
import { agentApiBase } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';
import type { AccountIdentity } from './accounts';

async function send<T>(method: 'POST' | 'PUT', path: string, body: object): Promise<T> {
  return jsonOrThrow<T>(
    await fetchWithAuth(agentApiBase, `/v1/auth/account${path}`, {
      method,
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  );
}

/** A proof of the owner: the current password, or a code sent to the verified phone. */
export interface OwnerProof {
  password?: string;
  phone_code?: string;
}

export async function getOwnAccount(signal?: AbortSignal): Promise<AccountIdentity> {
  return jsonOrThrow<AccountIdentity>(
    await fetchWithAuth(agentApiBase, '/v1/auth/account', { cache: 'no-store', signal }),
  );
}
export const changePassword = (details: {
  old_password?: string;
  phone_code?: string;
  new_password: string;
}) => send<{ ok: boolean }>('POST', '/password', details);
export const requestEmailBind = (email: string, proof: OwnerProof) =>
  send<{ email: string; email_verified: boolean }>('POST', '/email', { email, ...proof });
export const bindPhone = (phone: string, code: string) =>
  send<AccountIdentity>('POST', '/phone', { phone, code });
export const deleteOwnAccount = (confirm: string, proof: OwnerProof) =>
  send<{ ok: boolean }>('POST', '/delete', { confirm, ...proof });
export const saveAccountLocale = (locale: string, chosenAt?: string) =>
  send<{ locale: string | null; chosen_at: string | null }>('PUT', '/locale', {
    locale,
    ...(chosenAt ? { chosen_at: chosenAt } : {}),
  });
