import { afterEach, describe, expect, it, vi } from 'vitest';

import { getSandboxArchiveSettings, updateSandboxArchiveSettings } from './adminArchiveSettings';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

const settings = {
  enabled: false,
  s3_uri: 's3://freeinference/cloud-agent/sandbox',
  region: 'us-east-1',
  kms_key_arn: 'arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000',
  expected_bucket_owner: '123456789012',
  aws_access_key_id: '',
  aws_secret_access_key_configured: false,
  max_archive_bytes: 2_147_483_648,
  multipart_threshold_bytes: 8_388_608,
  multipart_part_bytes: 8_388_608,
  revision: 3,
  updated_at: '2026-10-10T10:00:00Z',
};

describe('admin sandbox archive settings client', () => {
  it('reads the admin-only settings without caching and supports abort', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(
        new Response(JSON.stringify(settings), { headers: { 'Content-Type': 'application/json' } }),
      );
    const controller = new AbortController();

    await expect(getSandboxArchiveSettings(controller.signal)).resolves.toEqual(settings);
    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      '/admin/agent/sandbox-archive-settings',
      {
        signal: controller.signal,
        cache: 'no-store',
      },
    );
  });

  it('atomically writes the configurable fields and revision without the fixed URI', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
      new Response(JSON.stringify({ ...settings, enabled: true, revision: 4 }), {
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const update = {
      enabled: true,
      region: settings.region,
      kms_key_arn: settings.kms_key_arn,
      expected_bucket_owner: settings.expected_bucket_owner,
      aws_access_key_id: '',
      aws_secret_access_key: '',
      max_archive_bytes: settings.max_archive_bytes,
      multipart_threshold_bytes: settings.multipart_threshold_bytes,
      multipart_part_bytes: settings.multipart_part_bytes,
      revision: settings.revision,
      checkpoints_enabled: false,
    };

    await updateSandboxArchiveSettings(update);

    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      '/admin/agent/sandbox-archive-settings',
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(update),
      },
    );
    expect(JSON.parse(fetch.mock.calls[0][2]?.body as string)).not.toHaveProperty('s3_uri');
  });
});
