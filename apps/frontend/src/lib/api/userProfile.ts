import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';
import { announceProfileSaved } from './profileEvents';

const PROFILE = '/v1/agent/profile';

export interface UserProfile {
  email: string;
  first_name: string;
  last_name: string;
  image_data_url: string | null;
  revision: number;
  /** The account's fixed Pokémon companion, shown when no photo is saved. */
  companion_data_url: string;
}

export interface UserProfileUpdate {
  first_name: string;
  last_name: string;
  expected_revision: number;
  /** Omit to keep the saved picture; null removes it. */
  image_data_url?: string | null;
}

export async function getUserProfile(signal?: AbortSignal): Promise<UserProfile> {
  return jsonOrThrow<UserProfile>(
    await fetchWithAuth(config.apiBase, PROFILE, { signal, cache: 'no-store' }),
  );
}

export async function saveUserProfile(
  update: UserProfileUpdate,
  signal?: AbortSignal,
): Promise<UserProfile> {
  const saved = await jsonOrThrow<UserProfile>(
    await fetchWithAuth(config.apiBase, PROFILE, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(update),
      signal,
    }),
  );
  if (!signal?.aborted) announceProfileSaved(saved);
  return saved;
}
