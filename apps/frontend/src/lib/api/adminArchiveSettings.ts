import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

const SANDBOX_ARCHIVE_SETTINGS = '/admin/agent/sandbox-archive-settings';

/** A host with this capability accepts database-backed archive policy. */
export const SANDBOX_ARCHIVE_SETTINGS_CAPABILITY = 'sandbox-archive-settings-v1';

/** Deployment-wide archive settings stored by the control plane. */
export interface SandboxArchiveSettings {
  enabled: boolean;
  s3_uri: string;
  region: string;
  kms_key_arn: string;
  expected_bucket_owner: string;
  max_archive_bytes: number;
  multipart_threshold_bytes: number;
  multipart_part_bytes: number;
  revision: number;
  updated_at: string | null;
}

/**
 * The destination URI is deliberately absent: the service owns the only
 * permitted bucket and prefix, while an admin controls whether and how it is
 * used. `revision` makes replacement of the related fields atomic.
 */
export interface SandboxArchiveSettingsUpdate {
  enabled: boolean;
  region: string;
  kms_key_arn: string;
  expected_bucket_owner: string;
  max_archive_bytes: number;
  multipart_threshold_bytes: number;
  multipart_part_bytes: number;
  revision: number;
}

export async function getSandboxArchiveSettings(
  signal?: AbortSignal,
): Promise<SandboxArchiveSettings> {
  const response = await fetchWithAuth(config.apiBase, SANDBOX_ARCHIVE_SETTINGS, {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<SandboxArchiveSettings>(response);
}

export async function updateSandboxArchiveSettings(
  settings: SandboxArchiveSettingsUpdate,
): Promise<SandboxArchiveSettings> {
  const response = await fetchWithAuth(config.apiBase, SANDBOX_ARCHIVE_SETTINGS, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  });
  return jsonOrThrow<SandboxArchiveSettings>(response);
}
