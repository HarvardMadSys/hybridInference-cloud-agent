import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

const SANDBOX_ARCHIVE_SETTINGS = '/admin/agent/sandbox-archive-settings';

/** A host with this capability accepts database-backed archive policy. */
export const SANDBOX_ARCHIVE_SETTINGS_CAPABILITY = 'sandbox-archive-settings-v1';

/** A host with this capability archives to a bucket other than the default. */
export const SANDBOX_ARCHIVE_DESTINATION_CAPABILITY = 'sandbox-archive-destination-v1';

/** Every archive key starts with this prefix. Only the bucket is a setting. */
export const SANDBOX_ARCHIVE_PREFIX = 'cloud-agent/sandbox';

export const DEFAULT_SANDBOX_ARCHIVE_URI = `s3://freeinference/${SANDBOX_ARCHIVE_PREFIX}`;

/** Deployment-wide archive settings stored by the control plane. */
export interface SandboxArchiveSettings {
  enabled: boolean;
  s3_uri: string;
  region: string;
  kms_key_arn: string;
  expected_bucket_owner: string;
  aws_access_key_id: string;
  aws_secret_access_key_configured: boolean;
  max_archive_bytes: number;
  multipart_threshold_bytes: number;
  multipart_part_bytes: number;
  revision: number;
  updated_at: string | null;
  /**
   * Clean stops also store a restorable checkpoint. On by default with
   * archives; absent from older APIs.
   */
  checkpoints_enabled?: boolean;
}

/**
 * `s3_uri` is `s3://<bucket>/cloud-agent/sandbox`: the bucket is a setting,
 * the prefix is not. Leaving it out keeps the stored destination. `revision`
 * makes replacement of the related fields atomic.
 */
export interface SandboxArchiveSettingsUpdate {
  enabled: boolean;
  s3_uri?: string;
  region: string;
  kms_key_arn: string;
  expected_bucket_owner: string;
  aws_access_key_id: string;
  aws_secret_access_key: string;
  max_archive_bytes: number;
  multipart_threshold_bytes: number;
  multipart_part_bytes: number;
  revision: number;
  checkpoints_enabled: boolean;
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
